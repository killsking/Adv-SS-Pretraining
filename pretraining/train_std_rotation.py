import os
import pdb
import torch
import pickle
import argparse
import random
import numpy as np  
import PIL.Image as Image
import matplotlib.pyplot as plt

import torch.nn as nn
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torchvision.transforms as transforms
import torchvision.datasets as datasets
from torch.autograd import Variable
import torchvision.models as models
import torch.nn.functional as F
from torch.utils.data.sampler import SubsetRandomSampler

from resnetv2 import ResNet50 as Rotation_model
from advertorch.utils import NormalizeByChannelMeanStd

parser = argparse.ArgumentParser(description='PyTorch Cifar10 Training')
parser.add_argument('--batch_size', type=int, default=128, help='batch size')
parser.add_argument('--lr', default=0.1, type=float, help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, help='momentum')
parser.add_argument('--weight_decay', default=3e-4, type=float, help='weight decay')
parser.add_argument('--epochs', default=200, type=int, help='number of total epochs to run')
parser.add_argument('--print_freq', default=50, type=int, help='print frequency')
parser.add_argument('--data', type=str, default='/data4/zzy/data/', help='location of the data corpus')
parser.add_argument('--save_dir', help='The directory used to save the trained models', default='adv', type=str)
parser.add_argument('--gpu', type=int, default=0, help='gpu device id')
parser.add_argument('--seed', type=int, default=20, help='random seed')

best_prec1 = 0

def main():
    global args, best_prec1
    args = parser.parse_args()
    print(args)

    if not torch.cuda.is_available():
        logging.info('no gpu device available')
        sys.exit(1)
    torch.cuda.set_device(int(args.gpu))

    setup_seed(args.seed)

    model = Rotation_model(4)
    normalize = NormalizeByChannelMeanStd(
    mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    model = nn.Sequential(normalize, model)
    model.cuda()
    
    cudnn.benchmark = True

    train_trans = transforms.Compose([
            transforms.RandomHorizontalFlip(),
            transforms.RandomCrop(32, 4),
            transforms.ToTensor()
        ])

    val_trans = transforms.Compose([
            transforms.ToTensor()
        ])

    #dataset process
    train_dataset = datasets.CIFAR10(args.data, train=True, transform=train_trans, download=True)
    test_dataset = datasets.CIFAR10(args.data, train=False, transform=val_trans, download=True)


    valid_size = 0.1
    indices = list(range(len(train_dataset)))
    split = int(np.floor(valid_size*len(train_dataset)))
    np.random.shuffle(indices)

    train_idx, valid_idx = indices[split:], indices[:split]
    train_sampler = torch.utils.data.Subset(train_dataset, train_idx)
    valid_sampler = torch.utils.data.Subset(train_dataset, valid_idx)

    train_loader = torch.utils.data.DataLoader(
        train_sampler,
        batch_size=args.batch_size, shuffle=True,
        num_workers=2, pin_memory=True)

    val_loader = torch.utils.data.DataLoader(
        valid_sampler,
        batch_size=args.batch_size, shuffle=False,
        num_workers=2, pin_memory=True)

    test_loader = torch.utils.data.DataLoader(
        test_dataset,
        batch_size=args.batch_size, shuffle=False,
        num_workers=2, pin_memory=True)

    criterion = nn.CrossEntropyLoss()
    criterion = criterion.cuda()

    optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)
                                
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer,
        lr_lambda=lambda step: cosine_annealing(
            step,
            args.epochs * len(train_loader),
            1,  # since lr_lambda computes multiplicative factor
            1e-6 / args.lr))

    print('std training')
    train_acc=[]
    ta=[]

    if os.path.exists(args.save_dir) is not True:
        os.mkdir(args.save_dir)


    for epoch in range(args.epochs):

        print(optimizer.state_dict()['param_groups'][0]['lr'])
        acc,loss = train(train_loader, model, criterion, optimizer, epoch, scheduler)

        # evaluate on validation set
        tacc,tloss = validate(val_loader, model, criterion)

        train_acc.append(acc)
        ta.append(tacc)


        # remember best prec@1 and save checkpoint
        is_best = tacc  > best_prec1
        best_prec1 = max(tacc, best_prec1)

        
        if is_best:

            save_checkpoint({
                'epoch': epoch + 1,
                'state_dict': model.state_dict(),
                'best_prec1': best_prec1,
            }, is_best, filename=os.path.join(args.save_dir, 'best_model.pt'))

        save_checkpoint({
            'epoch': epoch + 1,
            'state_dict': model.state_dict(),
            'best_prec1': best_prec1,
        }, is_best, filename=os.path.join(args.save_dir, 'model.pt'))
    
        plt.plot(train_acc, label='train_acc')
        plt.plot(ta, label='TA')
        plt.legend()
        plt.savefig(os.path.join(args.save_dir, 'net_train.png'))
        plt.close()

    model_path = os.path.join(args.save_dir, 'best_model.pt')
    model.load_state_dict(torch.load(model_path)['state_dict'])
    print('testing result of ta best model')
    tacc,tloss = validate(test_loader, model, criterion)

        

def train(train_loader, model, criterion, optimizer, epoch, scheduler):
    
    losses = AverageMeter()
    top1 = AverageMeter()

    # switch to train mode
    model.train()

    for i, (input, target) in enumerate(train_loader):

        #warm up
        if epoch == 0:
            warmup_lr(i, optimizer, 200)

        input,target_rot = rotation(input)

        input = input.cuda()
        target = target_rot.cuda()

        # compute output
        output_clean = model(input)
        loss = criterion(output_clean, target)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        scheduler.step()

        output = output_clean.float()
        loss = loss.float()
        # measure accuracy and record loss
        prec1 = accuracy(output.data, target)[0]

        losses.update(loss.item(), input.size(0))
        top1.update(prec1.item(), input.size(0))


        if i % args.print_freq == 0:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Accuracy {top1.val:.3f} ({top1.avg:.3f})\t'.format(
                      epoch, i, len(train_loader), loss=losses, top1=top1))

    print('train_accuracy {top1.avg:.3f}'.format(top1=top1))

    return top1.avg, losses.avg
    
def validate(val_loader, model, criterion):
    """
    Run evaluation
    """
    losses = AverageMeter()
    top1 = AverageMeter()

    # switch to evaluate mode
    model.eval()

    for i, (input, target) in enumerate(val_loader):

        input, target_rot = rotation(input)

        input = input.cuda()
        target = target_rot.cuda()

        # compute output
        with torch.no_grad():
            output = model(input)
            loss = criterion(output, target)

        output = output.float()
        loss = loss.float()

        # measure accuracy and record loss
        prec1 = accuracy(output.data, target)[0]
        losses.update(loss.item(), input.size(0))
        top1.update(prec1.item(), input.size(0))

        if i % args.print_freq == 0:
            print('Test: [{0}/{1}]\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Accuracy {top1.val:.3f} ({top1.avg:.3f})'.format(
                      i, len(val_loader), loss=losses, top1=top1))

    print('valid_accuracy {top1.avg:.3f}'
          .format(top1=top1))

    return top1.avg, losses.avg

def save_checkpoint(state, is_best, filename='weight.pt'):
    """
    Save the training model
    """
    torch.save(state, filename)

class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

def warmup_lr(step, optimizer, speed):
    lr = 0.01+step*(0.1-0.01)/speed
    lr = min(lr,0.1)
    for p in optimizer.param_groups:
        p['lr']=lr

def setup_seed(seed): 
    torch.manual_seed(seed) 
    torch.cuda.manual_seed_all(seed) 
    np.random.seed(seed) 
    random.seed(seed) 
    torch.backends.cudnn.deterministic = True 

def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res

def cosine_annealing(step, total_steps, lr_max, lr_min):
    return lr_min + (lr_max - lr_min) * 0.5 * (
            1 + np.cos(step / total_steps * np.pi))

def rotation(input):
    batch = input.shape[0]
    target = torch.tensor(np.random.permutation([0,1,2,3] * (int(batch / 4) + 1)), device = input.device)[:batch]
    target = target.long()
    image = torch.zeros_like(input)
    image.copy_(input)
    for i in range(batch):
        image[i, :, :, :] = torch.rot90(input[i, :, :, :], target[i], [1, 2])

    return image, target

if __name__ == '__main__':
    main()


