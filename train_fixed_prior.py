import os
import torch
import random
import argparse
import itertools
import numpy as np
from tqdm import tqdm
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.autograd import Variable
from torch.utils.data import DataLoader
from models.lstm import gaussian_lstm, lstm
from dataset import bair_robot_pushing_dataset
from models.vgg_64 import vgg_decoder, vgg_encoder
from utils import init_weights, kl_criterion, plot_pred, plot_rec, finn_eval_seq, pred, plot_curve

import sys

torch.backends.cudnn.benchmark = True


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--lr', default=0.002, type=float, help='learning rate')
    parser.add_argument('--beta1', default=0.9, type=float, help='momentum term for adam')
    parser.add_argument('--batch_size', default=24, type=int, help='batch size')
    parser.add_argument('--log_dir', default='./logs/fp', help='base directory to save logs')
    # parser.add_argument('--model_dir', default='./logs/fp/b', help='base directory to save logs')
    parser.add_argument('--model_dir', default='', help='base directory to save logs')
    parser.add_argument('--exp_name', type=str, default='f')#
    parser.add_argument('--data_root', default='./data', help='root directory for data')
    parser.add_argument('--optimizer', default='adam', help='optimizer to train with')
    parser.add_argument('--niter', type=int, default=100, help='number of epochs to train for')
    parser.add_argument('--epoch_size', type=int, default=600, help='epoch size')
    parser.add_argument('--tfr', type=float, default=0.0, help='teacher forcing ratio (0 ~ 1)')#
    parser.add_argument('--tfr_start_decay_epoch', type=int, default=1000, help='The epoch that teacher forcing ratio become decreasing')#
    parser.add_argument('--tfr_decay_step', type=float, default=0.0, help='The decay step size of teacher forcing ratio (0 ~ 1)')#
    parser.add_argument('--tfr_lower_bound', type=float, default=0.0, help='The lower bound of teacher forcing ratio for scheduling teacher forcing ratio (0 ~ 1)')
    parser.add_argument('--kl_anneal_cyclical', default=False, action='store_true', help='use cyclical mode')
    parser.add_argument('--kl_anneal_ratio', type=float, default=0.0, help='The decay ratio of kl annealing') # not used here
    parser.add_argument('--kl_anneal_cycle', type=int, default=4, help='The number of cycle for kl annealing (if use cyclical mode)')
    parser.add_argument('--seed', default=1, type=int, help='manual seed')
    parser.add_argument('--n_past', type=int, default=2, help='number of frames to condition on')
    parser.add_argument('--n_future', type=int, default=10, help='number of frames to predict')
    parser.add_argument('--n_eval', type=int, default=30, help='number of frames to predict at eval time')
    parser.add_argument('--rnn_size', type=int, default=256, help='dimensionality of hidden layer')
    parser.add_argument('--posterior_rnn_layers', type=int, default=1, help='number of layers')
    parser.add_argument('--predictor_rnn_layers', type=int, default=2, help='number of layers')
    parser.add_argument('--z_dim', type=int, default=64, help='dimensionality of z_t')
    parser.add_argument('--g_dim', type=int, default=128, help='dimensionality of encoder output vector and decoder input vector')
    parser.add_argument('--beta', type=float, default=0.0001, help='weighting on KL to prior')
    parser.add_argument('--num_workers', type=int, default=1, help='number of data loading threads')
    parser.add_argument('--last_frame_skip', action='store_true', help='if true, skip connections go between frame t and frame t+t rather than last ground truth frame')
    parser.add_argument('--cuda', default=True, action='store_true')  

    args = parser.parse_args()
    return args


def train(x, cond, modules, mse_criterion, optimizer, kl_anneal, args):
    modules['frame_predictor'].zero_grad()
    modules['posterior'].zero_grad()
    modules['encoder'].zero_grad()
    modules['decoder'].zero_grad()

    # initialize the hidden state.
    modules['frame_predictor'].hidden = modules['frame_predictor'].init_hidden()
    modules['posterior'].hidden = modules['posterior'].init_hidden()
    h_seq = [modules['encoder'](x[i]) for i in range(args.n_past+args.n_future)]
    mse = 0
    kld = 0
    use_teacher_forcing = True if random.random() < args.tfr else False
    for i in range(1, args.n_past+args.n_future):
        h_target = h_seq[i][0]
        if args.last_frame_skip or i < args.n_past:	
            h, skip = h_seq[i-1]
        else:
            h = h_seq[i-1][0]
        z_t, mu, logvar = modules['posterior'](h_target)
        h_pred = modules['frame_predictor'](torch.cat([h, z_t, cond[i-1]], 1))
        x_pred = modules['decoder']([h_pred, skip])
        mse += mse_criterion(x_pred, x[i])
        kld += kl_criterion(mu, logvar, args)
        if not use_teacher_forcing:
            h_seq[i] = modules['encoder'](x_pred)

    beta = kl_anneal.get_beta()
    loss = mse + kld * beta
    loss.backward()

    optimizer.step()

    return loss.detach().cpu().numpy() / (args.n_past + args.n_future), mse.detach().cpu().numpy() / (args.n_past + args.n_future), kld.detach().cpu().numpy() / (args.n_future + args.n_past)


class kl_annealing():
    def __init__(self, args):
        super().__init__()
        self.n_iter = args.niter
        self.n_cycle = args.kl_anneal_cycle
        self.cyclical_mode = args.kl_anneal_cyclical
        self.i = 0
        if self.cyclical_mode:
            self.L = self.frange_cycle_linear(n_cycle=self.n_cycle)
        else:
            self.L = self.frange_cycle_linear(n_cycle=1, ratio=0.25)

    
    def frange_cycle_linear(self, start=0.0, stop=1.0,  n_cycle=4, ratio=0.5):
        L = np.ones(self.n_iter) * stop
        period = self.n_iter/n_cycle # number of iterations per cycle
        step = (stop-start)/(period*ratio)

        for c in range(n_cycle):
            v, i = start, 0
            while v <= stop and (int(i+c*period) < self.n_iter):
                L[int(i+c*period)] = v
                v += step
                i += 1
        return L

    def update(self):
        self.i += 1
    
    def get_beta(self):
        return self.L[self.i]


if __name__ == '__main__':
    args = parse_args()
    if args.cuda:
        assert torch.cuda.is_available(), 'CUDA is not available.'
        device = 'cuda'
    else:
        device = 'cpu'
    
    assert args.n_past + args.n_future <= 30 and args.n_eval <= 30
    assert 0 <= args.tfr and args.tfr <= 1
    assert 0 <= args.tfr_start_decay_epoch 
    assert 0 <= args.tfr_decay_step and args.tfr_decay_step <= 1

    if args.model_dir != '':
        # load model and continue training from checkpoint
        saved_model = torch.load('%s/model_112.pth' % args.model_dir)
        optimizer = args.optimizer
        model_dir = args.model_dir
        niter = args.niter
        args = saved_model['args']
        args.optimizer = optimizer
        args.model_dir = model_dir
        # args.log_dir = '%s/continued' % args.log_dir
        start_epoch = saved_model['last_epoch'] + 1
        print(f'Using pre-trained: {args.model_dir}/model_112.pth')

    else:
        # name = 'rnn_size=%d-predictor-posterior-rnn_layers=%d-%d-n_past=%d-n_future=%d-lr=%.4f-g_dim=%d-z_dim=%d-last_frame_skip=%s-beta=%.7f'\
        #     % (args.rnn_size, args.predictor_rnn_layers, args.posterior_rnn_layers, args.n_past, args.n_future, args.lr, args.g_dim, args.z_dim, args.last_frame_skip, args.beta)
        name = args.exp_name
        args.log_dir = '%s/%s' % (args.log_dir, name)
        niter = args.niter
        start_epoch = 0

    os.makedirs(args.log_dir, exist_ok=True)
    os.makedirs('%s/gen/' % args.log_dir, exist_ok=True)

    print("Random Seed: ", args.seed)
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)



    if os.path.exists('./{}/train_record.txt'.format(args.log_dir)):
        # os.remove('./{}/train_record.txt'.format(args.log_dir))
        with open('./{}/train_record.txt'.format(args.log_dir), 'a') as train_record:
            train_record.write('==============continue================\n')
    else:
        with open('./{}/train_record.txt'.format(args.log_dir), 'a') as train_record:
            train_record.write('args: {}\n'.format(args))
    print(args)

    # with open('./{}/train_record.txt'.format(args.log_dir), 'a') as train_record:
    #     train_record.write('args: {}\n'.format(args))

    # ------------ build the models  --------------

    if args.model_dir != '':
        frame_predictor = saved_model['frame_predictor']
        posterior = saved_model['posterior']
    else:
        frame_predictor = lstm(args.g_dim+args.z_dim+7, args.g_dim, args.rnn_size, args.predictor_rnn_layers, args.batch_size, device)
        posterior = gaussian_lstm(args.g_dim, args.z_dim, args.rnn_size, args.posterior_rnn_layers, args.batch_size, device)
        frame_predictor.apply(init_weights)
        posterior.apply(init_weights)
            
    if args.model_dir != '':
        decoder = saved_model['decoder']
        encoder = saved_model['encoder']
    else:
        encoder = vgg_encoder(args.g_dim)
        decoder = vgg_decoder(args.g_dim)
        encoder.apply(init_weights)
        decoder.apply(init_weights)

    # --------- transfer to device ------------------------------------
    frame_predictor.to(device)
    posterior.to(device)
    encoder.to(device)
    decoder.to(device)

    # --------- load a dataset ------------------------------------
    train_data = bair_robot_pushing_dataset(args, 'train')
    validate_data = bair_robot_pushing_dataset(args, 'validate')
    train_loader = DataLoader(train_data,
                            num_workers=args.num_workers,
                            batch_size=args.batch_size,
                            shuffle=True,
                            drop_last=True,
                            pin_memory=True)
    train_iterator = iter(train_loader)

    validate_loader = DataLoader(validate_data,
                            num_workers=args.num_workers,
                            batch_size=args.batch_size,
                            shuffle=True,
                            drop_last=True,
                            pin_memory=True)

    validate_iterator = iter(validate_loader)

    # ---------------- optimizers ----------------
    if args.optimizer == 'adam':
        args.optimizer = optim.Adam
    elif args.optimizer == 'rmsprop':
        args.optimizer = optim.RMSprop
    elif args.optimizer == 'sgd':
        args.optimizer = optim.SGD
    else:
        raise ValueError('Unknown optimizer: %s' % args.optimizer)

    params = list(frame_predictor.parameters()) + list(posterior.parameters()) + list(encoder.parameters()) + list(decoder.parameters())
    optimizer = args.optimizer(params, lr=args.lr, betas=(args.beta1, 0.999))
    kl_anneal = kl_annealing(args)
    mse_criterion = nn.MSELoss()
    mse_criterion.to(device)

    modules = {
        'frame_predictor': frame_predictor,
        'posterior': posterior,
        'encoder': encoder,
        'decoder': decoder,
    }
    # --------- training loop ------------------------------------

    progress = tqdm(total=niter)
    best_val_psnr = 0
    all_epoch_loss = []
    all_epoch_mse = []
    all_epoch_kld = []
    all_tf_ratios = []
    all_kl_ratios = []
    all_ave_psnr = []
    # args.epoch_size = len(train_loader)
    for epoch in range(start_epoch, start_epoch + niter):
        frame_predictor.train()
        posterior.train()
        encoder.train()
        decoder.train()

        epoch_mse = 0
        epoch_kld = 0
        epoch_loss = 0


        for _ in tqdm(range(args.epoch_size), desc="[Epoch {}]".format(epoch)):
            try:
                ## Train on next batch
                seq, cond = next(train_iterator)
            except StopIteration:
                ## If all batches have been trained, return to the first batch
                train_iterator = iter(train_loader)
                seq, cond = next(train_iterator)
            ## Swap axes of batch & frames
            seq  = seq.permute((1, 0, 2, 3, 4)).to(device)
            cond = cond.permute((1, 0, 2)).to(device)
            loss, mse, kld = train(seq[:12], cond[:12], modules, mse_criterion, optimizer, kl_anneal, args)
            epoch_mse += mse
            epoch_kld += kld
            epoch_loss += loss

        bias = 0.0001
        if epoch >= args.tfr_start_decay_epoch:
            ### Update teacher forcing ratio ###
            if args.tfr > args.tfr_lower_bound+bias:
                args.tfr = args.tfr - args.tfr_decay_step
        
        all_tf_ratios.append(args.tfr)
        all_kl_ratios.append(kl_anneal.get_beta())
        all_epoch_mse.append(epoch_mse / args.epoch_size)
        all_epoch_kld.append(epoch_kld / args.epoch_size)
        all_epoch_loss.append(epoch_loss / args.epoch_size)

        print(f'TF ratio: {args.tfr}')
        print(f'KL weight: {kl_anneal.get_beta()}')
        print(f'loss: {epoch_loss  / args.epoch_size}')
        print(f'mse loss: {epoch_mse  / args.epoch_size}')
        print(f'kld loss: {epoch_kld  / args.epoch_size}')
        kl_anneal.update()
        progress.update(1)

        with open('./{}/train_record.txt'.format(args.log_dir), 'a') as train_record:
            train_record.write((f'[epoch: %02d] loss: %.5f | mse loss: %.5f | kld loss: %.5f\n' % (epoch, epoch_loss  / args.epoch_size, epoch_mse / args.epoch_size, epoch_kld / args.epoch_size)))
        
        frame_predictor.eval()
        encoder.eval()
        decoder.eval()
        posterior.eval()

        if epoch % 1 == 0:
            psnr_list = []    
            for _ in range(len(validate_data) // args.batch_size):
                try:
                    validate_seq, validate_cond = next(validate_iterator)
                except StopIteration:
                    validate_iterator = iter(validate_loader)
                    validate_seq, validate_cond = next(validate_iterator)

                validate_seq = validate_seq.permute(1, 0, 2, 3, 4).to(device)
                validate_cond = validate_cond.permute(1, 0, 2).to(device)

                pred_seq = pred(validate_seq[:12], validate_cond[:12], modules, args)
                _, _, psnr = finn_eval_seq(validate_seq[args.n_past:12], pred_seq[args.n_past:12])
                psnr_list.append(psnr)
                
            ave_psnr = np.mean(np.concatenate(psnr_list))
            all_ave_psnr.append(ave_psnr)

            with open('./{}/train_record.txt'.format(args.log_dir), 'a') as train_record:
                train_record.write(f'====================== validate psnr = {ave_psnr:.5f} ========================\n')

            if ave_psnr > best_val_psnr:
                best_val_psnr = ave_psnr
                # save the model
                torch.save({
                    'encoder': encoder,
                    'decoder': decoder,
                    'frame_predictor': frame_predictor,
                    'posterior': posterior,
                    'args': args,
                    'last_epoch': epoch},
                    f'{args.log_dir}/model_{epoch}.pth')

        if epoch % 10 == 0:
            try:
                validate_seq, validate_cond = next(validate_iterator)
            except StopIteration:
                validate_iterator = iter(validate_loader)
                validate_seq, validate_cond = next(validate_iterator)

            validate_seq = validate_seq.permute(1, 0, 2, 3, 4).to(device)
            validate_cond = validate_cond.permute(1, 0, 2).to(device)

            plot_pred(validate_seq, validate_cond, modules, epoch, args)
            plot_rec(validate_seq, validate_cond, modules, epoch, args)

    plot_curve(all_epoch_loss, all_epoch_mse, all_epoch_kld, all_tf_ratios, all_kl_ratios, all_ave_psnr, args)