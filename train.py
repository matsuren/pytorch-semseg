import sys, os
import torch
import visdom
import argparse
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models

from torch.autograd import Variable
from torch.utils import data
from tqdm import tqdm

from ptsemseg.models import get_model
from ptsemseg.loader import get_loader, get_data_path
from ptsemseg.metrics import runningScore
from ptsemseg.loss import *
from ptsemseg.augmentations import *



def train(args):
    cpu = args.gpu < 0
    if not cpu:
        torch.cuda.set_device(args.gpu)

# Setup Augmentations
    data_aug= Compose([RandomRotate(10),                                        
                       RandomHorizontallyFlip()])

    # Setup Dataloader
    data_loader = get_loader(args.dataset)
    data_path = get_data_path(args.dataset)
    t_loader = data_loader(data_path, is_transform=True, img_size=(args.img_rows, args.img_cols), augmentations=data_aug, img_norm=args.img_norm)
    v_loader = data_loader(data_path, is_transform=True, split='val', img_size=(args.img_rows, args.img_cols), img_norm=args.img_norm)

    n_classes = t_loader.n_classes
    trainloader = data.DataLoader(t_loader, batch_size=args.batch_size, num_workers=8, shuffle=True)
    valloader = data.DataLoader(v_loader, batch_size=args.batch_size, num_workers=8)

    # Setup Metrics
    running_metrics = runningScore(n_classes)
        
    # Setup visdom for visualization
    if args.visdom:
        vis = visdom.Visdom()

        loss_window = vis.line(X=torch.zeros((1,)).cpu(),
                           Y=torch.zeros((1)).cpu(),
                           opts=dict(xlabel='minibatches',
                                     ylabel='Loss',
                                     title=args.arch + ' Training Loss '+ args.dataset,
                                     legend=['Loss']))
        acc_window = vis.line(X=np.zeros((1, 4)),
                              Y=np.zeros((1, 4)),
                              opts=dict(xlabel='epoch',
                                        ylabel='Accuracy',
                                        title=args.arch + ' Accuracy '+ args.dataset,
                                        legend=['acc', 'acc_cls', 'fwavacc', 'mean_iu']))
    # Setup Model
    model = get_model(args.arch, n_classes)
    
    # model = torch.nn.DataParallel(model, device_ids=range(torch.cuda.device_count()))
    model = torch.nn.DataParallel(model, device_ids=[args.gpu], output_device=args.gpu)
    if cpu:
        model.cpu()
    else:
        model.cuda()
    
    # Check if model has custom optimizer / loss
    if hasattr(model.module, 'optimizer'):
        optimizer = model.module.optimizer
    else:
        optimizer = torch.optim.SGD(model.parameters(), lr=args.l_rate, momentum=0.99, weight_decay=5e-4)

    if hasattr(model.module, 'loss'):
        print('Using custom loss')
        loss_fn = model.module.loss
    else:
        loss_fn = cross_entropy2d

    if args.resume is not None:                                         
        if os.path.isfile(args.resume):
            print("Loading model and optimizer from checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            model.load_state_dict(checkpoint['model_state'])
            optimizer.load_state_dict(checkpoint['optimizer_state'])
            print("Loaded checkpoint '{}' (epoch {})"                    
                  .format(args.resume, checkpoint['epoch']))
        else:
            print("No checkpoint found at '{}'".format(args.resume)) 

    best_iou = -100.0
    dataset_len = len(trainloader)
    for epoch in range(args.n_epoch):
        model.train()
        for i, (images, labels) in enumerate(trainloader):
            if cpu:
                images = Variable(images)
                labels = Variable(labels)
            else:
                images = Variable(images.cuda())
                labels = Variable(labels.cuda())

            optimizer.zero_grad()
            outputs = model(images)

            loss = loss_fn(input=outputs, target=labels)

            loss.backward()
            optimizer.step()

            if args.visdom:
                vis.line(
                    X=torch.ones((1, 1)).cpu() * (i + dataset_len*epoch),
                    Y=torch.Tensor([loss.data[0]]).unsqueeze(0).cpu(),
                    win=loss_window,
                    update='append')

            if (i+1) % 20 == 0:
                print("Epoch [%d/%d] Loss: %.4f" % (epoch+1, args.n_epoch, loss.data[0]))

        model.eval()
        for i_val, (images_val, labels_val) in tqdm(enumerate(valloader)):
            if cpu:
                images_val = Variable(images_val, volatile=True)
                labels_val = Variable(labels_val, volatile=True)
            else:
                images_val = Variable(images_val.cuda(), volatile=True)
                labels_val = Variable(labels_val.cuda(), volatile=True)

            outputs = model(images_val)
            pred = outputs.data.max(1)[1].cpu().numpy()
            gt = labels_val.data.cpu().numpy()
            running_metrics.update(gt, pred)

        score, class_iou = running_metrics.get_scores()
        acc = [score['Overall Acc: \t'],
               score['Mean Acc : \t'],
               score['FreqW Acc : \t'],
               score['Mean IoU : \t']]

        for k, v in score.items():
            print(k, v)
        if args.visdom:
            vis.line(
                X=np.ones((1, 4)) * epoch,
                Y=np.array([acc]),
                win=acc_window,
                update='append')
        running_metrics.reset()

        if score['Mean IoU : \t'] >= best_iou:
            best_iou = score['Mean IoU : \t']
            state = {'epoch': epoch+1,
                     'model_state': model.state_dict(),
                     'optimizer_state' : optimizer.state_dict(),}
            torch.save(state, "{}_{}_best_model.pkl".format(args.arch, args.dataset))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Hyperparams')
    parser.add_argument('--arch', nargs='?', type=str, default='segnet',
                        help='Architecture to use [\'fcn8s, unet, segnet etc\']')
    parser.add_argument('--dataset', nargs='?', type=str, default='sunrgbd',
                        help='Dataset to use [\'pascal, camvid, ade20k etc\']')
    parser.add_argument('--img_rows', nargs='?', type=int, default=240,
                        help='Height of the input image')
    parser.add_argument('--img_cols', nargs='?', type=int, default=320,
                        help='Width of the input image')

    parser.add_argument('--img_norm', dest='img_norm', action='store_true', 
                        help='Enable input image scales normalization [0, 1] | True by default')
    parser.add_argument('--no-img_norm', dest='img_norm', action='store_false', 
                        help='Disable input image scales normalization [0, 1] | True by default')
    parser.set_defaults(img_norm=True)

    parser.add_argument('--n_epoch', nargs='?', type=int, default=100, 
                        help='# of the epochs')
    parser.add_argument('--batch_size', nargs='?', type=int, default=1, 
                        help='Batch Size')
    parser.add_argument('--l_rate', nargs='?', type=float, default=1e-5, 
                        help='Learning Rate')
    parser.add_argument('--feature_scale', nargs='?', type=int, default=1, 
                        help='Divider for # of features to use')
    parser.add_argument('--resume', nargs='?', type=str, default=None,    
                        help='Path to previous saved model to restart from')

    parser.add_argument('--visdom', dest='visdom', action='store_true', 
                        help='Enable visualization(s) on visdom | True by default')
    parser.add_argument('--no-visdom', dest='visdom', action='store_false', 
                        help='Disable visualization(s) on visdom | True by default')
    parser.set_defaults(visdom=True)
    parser.add_argument('--gpu', nargs='?', type=int, default=0,
                        help='GPU id')

    args = parser.parse_args()
    train(args)
