"""
_ainbow-memory
Copyright 2021-present NAVER Corp.
GPLv3
"""
# -*- coding: utf-8 -*-
import logging.config
import os
import random
import numpy as np
import pandas as pd
import torch
from collections import defaultdict
from torch import nn
from pseudo_main import MetaPseudo
from torch.utils.tensorboard import SummaryWriter
from torchvision import transforms

from augmentation import RandAugmentCIFAR
from configuration import config
from utils.augment import Cutout, select_autoaugment
from utils.data_loader import get_test_datalist, get_statistics
from utils.data_loader import get_train_datalist
from utils.method_manager import select_method
from pseudo_main import MetaPseudo

cifar100_mean = (0.507075, 0.486549, 0.440918)
cifar100_std = (0.267334, 0.256438, 0.276151)
cifar10_mean = (0.491400, 0.482158, 0.4465231)
cifar10_std = (0.247032, 0.243485, 0.2615877)
normal_mean = (0.5, 0.5, 0.5)
normal_std = (0.5, 0.5, 0.5)
cifar_mean = 0
cifar_std = 0

class TransformMPL(object):
    def __init__(self, args, mean, std):
        if args.randaug:
            n, m = args.randaug
        else:
            n, m = 2, 10  # default

        self.ori = transforms.Compose([
            transforms.RandomHorizontalFlip(),
            transforms.RandomCrop(size=args.resize,
                                  padding=int(args.resize * 0.125),
                                  fill=128,
                                  padding_mode='constant')])
        self.aug = transforms.Compose([
            transforms.RandomHorizontalFlip(),
            transforms.RandomCrop(size=args.resize,
                                  padding=int(args.resize * 0.125),
                                  fill=128,
                                  padding_mode='constant'),
            RandAugmentCIFAR(n=n, m=m)])
        self.normalize = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(mean=mean, std=std)])

    def __call__(self, x):
        ori = self.ori(x)
        aug = self.aug(x)
        return self.normalize(ori), self.normalize(aug)


def make_blurry(major_ratio, num_labeled, num_classes):
    train = pd.read_json('./dataset/train_json.json')
    test = pd.read_json('./dataset/test_json.json')
    rnd_seed = 3  # random seed
    num_tasks = 5  # the number of tasks.
    np.random.seed(rnd_seed)

    klass = train.klass.unique()
    num_cls_per_task = len(klass) // num_tasks
    # print(num_cls_per_task) # cifar10????????? 10/5 = 2
    np.random.shuffle(klass)
    # enumerate : index ????????? class??? tuple??? ????????? ??????
    # house : 0, cat : 1 ??????????????? ???????????? ??????????????? ???
    class2label = {cls_: idx for idx, cls_ in enumerate(klass)}
    '''
    apply??? lambda
    lambda??? ?????? ????????? ???????????? ?????? ??????????????? ??????. ???????????? x??? class2label[x]??? ???????????? ????????? ????????? ????????? ???????????? ???????????????.
    ?????? x??? 0~9????????? ????????? ?????? ????????? ?????????, ??? class??? ???????????? ?????? matching ?????????
    ?????? ???????????? label????????? column??? ????????? matching ?????????
    '''
    train["label"] = train.klass.apply(lambda x: class2label[x])
    test["label"] = test.klass.apply(lambda x: class2label[x])

    task_class = np.split(klass, num_tasks)  # task?????? class??? ????????? (2??????!!)

    # list comprehension
    '''
    [(????????? ????????? ??????) for (????????? ?????? ??????) in (????????? ??? ?????? ???)]
    '''
    # task?????? ?????? ??? (isin??? ????????? tc??? ?????? ????????? true, ????????? false??? ???????????? ??????, train[true]??? ???????????? ?????????)
    # ?????? ????????? task?????? train data??? test data?????? ?????? ????????? ????????? ??? ??????.
    task_train = [train[train.klass.isin(tc)] for tc in task_class]
    task_test = [test[test.klass.isin(tc)] for tc in task_class]
    # major_ratio = 0.9 # 0.9 for blurry-10, 0.7 for blurry-30.
    # num_labeled = 4000 # cifar10 train total 50000
    # num_classes = 10 # cifar10
    major_classes = []
    label_per_class = num_labeled // num_classes

    task_trainM = []
    task_trainN = []
    for t in task_train:
        major_classes.append(list(t.klass.unique()))
        sub_task_trainN = []

        # sample ????????? ????????? M%??? ???????????? data ??????
        taskM = t.sample(n=int(len(t) * major_ratio), replace=False)
        taskN = pd.concat([taskM, t]).drop_duplicates(keep=False)
        taskN_size = len(taskN)

        task_trainM.append(taskM)

        # ????????? task?????? M/5%??? ??????
        for _ in range(len(task_train)-1):
            sub_task_trainN.append(taskN.sample(
                n=taskN_size//(len(task_train)-1)))

        task_trainN.append(sub_task_trainN)

    task_mixed_train = []
    for idx, task in enumerate(task_trainM):
        other_task_samples = pd.DataFrame()
        for j in range(len(task_trainM)):
            if idx != j:
                other_task_samples = pd.concat(
                    [other_task_samples, task_trainN[j].pop(0)])
        mixed_task = pd.concat([task, other_task_samples])
        task_mixed_train.append(mixed_task)

    labeled_idx = []
    total_data = []
    for idx, data_per_task in enumerate(task_mixed_train):
        major_class = major_classes[idx]
        for mc in major_class:
            # ?????? major class?????? labeled class ?????? (task_train[idx]?????????)
            index = list(np.where(task_train[idx].klass == mc)[0])
            # choice ?????? : index?????? label_per_class?????? choose?????? replace = False
            index = np.random.choice(index, label_per_class, False)
            labeled_data = []
            # labeled data??? ?????? ??????
            for i in index:
                labeled_data.append(data_per_task.iloc[i, :])
            # unlabeled data??? ?????? data
            unlabeled_data = data_per_task
            total_data.append((pd.DataFrame(labeled_data),
                              unlabeled_data, task_test[idx]))
    return total_data


def main():
    args = config.base_parser()

    # blurry 10??? ???????????? ????????? ??? parameter??? 0.9 / disjoint ????????? 1.0 ??????
    if args.dataset == "cifar10":
        num_class = 10
        cifar_mean = cifar10_mean
        cifar_std = cifar10_std
        
    elif args.dataset == "cifar100":
        num_class = 100
        cifar_mean = cifar100_mean
        cifar_std = cifar100_std

    else:
        num_class = 1000
    total_data = make_blurry(0.9, args.num_labeled, num_class)

    # make_blurry ?????? ??????
    # print("------------make_blurry------------")
    # print(len(total_data))

    # Save file name
    tr_names = ""
    for trans in args.transforms:
        tr_names += "_" + trans
    save_path = f"{args.dataset}/{args.mode}_{args.mem_manage}_{args.stream_env}_msz{args.memory_size}_rnd{args.rnd_seed}{tr_names}"

    logging.config.fileConfig("./configuration/logging.conf")

    # logger = logging.getLogger("name") ?????? logging instance ??????
    logger = logging.getLogger()

    os.makedirs(f"logs/{args.dataset}", exist_ok=True)
    # handler ?????? ??????
    fileHandler = logging.FileHandler(
        "logs/{}.log".format(save_path), mode="w")

    # formatter ?????? ??????
    formatter = logging.Formatter(
        "[%(levelname)s] %(filename)s:%(lineno)d > %(message)s"
    )

    # handler??? format ??????
    fileHandler.setFormatter(formatter)

    # logger??? ????????? handler ??????
    logger.addHandler(fileHandler)

    # pytorch??? tensor board??? ???????????? ???????????? summary writer instance??? ???????????? ??????.
    # writer = SummaryWriter("tensorboard")

    # gpu ?????? ??????
    if torch.cuda.is_available():
        device = torch.device("cuda")
    else:
        device = torch.device("cpu")
    logger.info(f"Set the device ({device})")

    # Fix the random seeds
    # https://hoya012.github.io/blog/reproducible_pytorch/
    torch.manual_seed(args.rnd_seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(args.rnd_seed)
    random.seed(args.rnd_seed)

    # Transform Definition
    # ??? dataset??? ????????? ??????, ???????????? ????????? ????????? ?????? ?????? ????????? ????????? ??????
    # mean, std, n_classes, inp_size, _ = get_statistics(dataset=args.dataset)

    n, m = 2, 10  # default

    transform_labeled = transforms.Compose([
        transforms.RandomHorizontalFlip(),
        transforms.RandomCrop(size=args.resize,
                              padding=int(args.resize * 0.125),
                              fill=128,
                              padding_mode='constant'),
        transforms.ToTensor(),
        transforms.Normalize(mean=cifar_mean, std=cifar_std),
    ])

    transform_unlabeled = TransformMPL(
        args, mean=cifar_mean, std=cifar_std)

    transform_finetune = transforms.Compose([
        transforms.RandomHorizontalFlip(),
        transforms.RandomCrop(size=args.resize,
                              padding=int(args.resize * 0.125),
                              fill=128,
                              padding_mode='constant'),
        RandAugmentCIFAR(n=n, m=m),
        transforms.ToTensor(),
        transforms.Normalize(mean=cifar_mean, std=cifar_std),
    ])

    transform_test = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize(mean=cifar_mean, std=cifar_std)
    ])

    logger.info(f"Using train-transforms {transform_labeled}")


    # Transform Definition
    mean, std, n_classes, inp_size, _ = get_statistics(dataset=args.dataset)
    train_transform = []
    if "cutout" in args.transforms:
        train_transform.append(Cutout(size=16))
    if "randaug" in args.transforms:
        train_transform.append(RandAugment())
    if "autoaug" in args.transforms:
        train_transform.append(select_autoaugment(args.dataset))

    rm_train_transform = transforms.Compose(
        [
            transforms.Resize((inp_size, inp_size)),
            transforms.RandomCrop(inp_size, padding=4),
            transforms.RandomHorizontalFlip(),
            *train_transform,
            transforms.ToTensor(),
            transforms.Normalize(mean, std),
        ]
    )
    logger.info(f"Using train-transforms {train_transform}")

    rm_test_transform = transforms.Compose(
        [
            transforms.Resize((inp_size, inp_size)),
            transforms.ToTensor(),
            transforms.Normalize(mean, std),
        ]
    )

    logger.info(f"[1] Select a CIL method ({args.mode})")
    criterion = nn.CrossEntropyLoss(reduction="mean")
    method = select_method(
        args, criterion, device, transform_labeled, transform_unlabeled, transform_test, 10, rm_train_transform, rm_test_transform
    )

    logger.info(f"[2] Incrementally training {args.n_tasks} tasks")
    task_records = defaultdict(list)

    for cur_iter in range(args.n_tasks):
        if args.mode == "joint" and cur_iter > 0:
            return

        print("\n" + "#" * 50)
        print(f"# Task {cur_iter} iteration")
        print("#" * 50 + "\n")
        logger.info("[2-1] Prepare a datalist for the current task")

        task_acc = 0.0
        eval_dict = dict()

        # get datalist
        '''
        cur_labeled_train_datalist, cur_unlabeled_train_datalist = get_train_datalist(
            args, cur_iter)
        cur_test_datalist = get_test_datalist(args, args.exp_name, cur_iter)
        '''
        cur_labeled_train_datalist = total_data[cur_iter][0]
        cur_unlabeled_train_datalist = total_data[cur_iter][1]
        cur_test_datalist = total_data[cur_iter][2]
        metapseudo = MetaPseudo(method,
                                args, cur_labeled_train_datalist, cur_unlabeled_train_datalist, cur_test_datalist, transform_labeled, transform_unlabeled, transform_test, rm_train_transform)

        pseudo_images, pseudo_targets = metapseudo.train_loop()
        # print("**********pseudo*********")
        # print("input_len", len(pseudo_images))
        # print("target_len", len(pseudo_targets))

        # Reduce datalist in Debug mode
        if args.debug:
            random.shuffle(cur_labeled_train_datalist)
            random.shuffle(cur_unlabeled_train_datalist)
            random.shuffle(cur_test_datalist)
            # debug mode????????? ????????? ??????
            # labeled data??? ????????? ???????????? ?????? ??? ??????
            cur_unlabeled_train_datalist = cur_unlabeled_train_datalist[:2560]
            cur_test_datalist = cur_test_datalist[:2560]

        logger.info("[2-2] Set environment for the current task")
        method.set_current_dataset(
            cur_labeled_train_datalist, cur_test_datalist, pseudo_images, pseudo_targets)
        # Increment known class for current task iteration.
        method.before_task(cur_labeled_train_datalist, cur_unlabeled_train_datalist,
                           cur_iter, args.init_model, args.init_opt)

        # The way to handle streamed samles
        logger.info(f"[2-3] Start to train under {args.stream_env}")

        if args.stream_env == "offline" or args.mode == "joint" or args.mode == "gdumb":
            # Offline Train
            task_acc, eval_dict = method.train(
                cur_iter=cur_iter,
                n_epoch=args.n_epoch,
                batch_size=args.batchsize,
                n_worker=16,
            )
            if args.mode == "joint":
                logger.info(f"joint accuracy: {task_acc}")

        elif args.stream_env == "online":
            # Online Train
            logger.info("Train over streamed data once")

            # meta pseudo label?????? ???????????? RM training
            # train
            method.train(
                cur_iter=cur_iter,
                n_epoch=1,
                batch_size=args.batchsize,
                n_worker=16,
            )

            method.update_memory(cur_iter)

            # No stremed training data, train with only memory_list
            method.set_current_dataset([], cur_test_datalist, [], [])

            logger.info("Train over memory")
            task_acc, eval_dict = method.train(
                cur_iter=cur_iter,
                n_epoch=args.n_epoch,
                batch_size=64,
                n_worker=16,
            )

            method.after_task(cur_iter)

        logger.info("[2-4] Update the information for the current task")
        method.after_task(cur_iter)
        task_records["task_acc"].append(task_acc)
        # task_records['cls_acc'][k][j] = break down j-class accuracy from 'task_acc'
        task_records["cls_acc"].append(eval_dict["cls_acc"])

        # Notify to NSML
        logger.info("[2-5] Report task result")
        #writer.add_scalar("Metrics/TaskAcc", task_acc, cur_iter)

    # np.save(f"results/{save_path}.npy", task_records["task_acc"])

    # Accuracy (A)
    A_avg = np.mean(task_records["task_acc"])
    A_last = task_records["task_acc"][args.n_tasks - 1]

    # Forgetting (F)
    acc_arr = np.array(task_records["cls_acc"])
    # cls_acc = (k, j), acc for j at k
    cls_acc = acc_arr.reshape(-1,
                              args.n_cls_a_task).mean(1).reshape(args.n_tasks, -1)
    for k in range(args.n_tasks):
        forget_k = []
        for j in range(args.n_tasks):
            if j < k:
                forget_k.append(cls_acc[:k, j].max() - cls_acc[k, j])
            else:
                forget_k.append(None)
        task_records["forget"].append(forget_k)
    F_last = np.mean(task_records["forget"][-1][:-1])

    # Intrasigence (I)
    I_last = args.joint_acc - A_last

    logger.info(f"======== Summary =======")
    logger.info(
        f"A_last {A_last} | A_avg {A_avg} | F_last {F_last} | I_last {I_last}")


if __name__ == "__main__":
    main()

