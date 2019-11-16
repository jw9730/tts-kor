import os
import sys
import math
import time
import torch
import random
import threading
import logging
from torch.utils.data import Dataset
import numpy as np
import librosa
from main import n_mfcc, n_frames

logger = logging.getLogger('root')
FORMAT = "[%(asctime)s %(filename)s:%(lineno)s - %(funcName)s()] %(message)s"
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG, format=FORMAT)
logger.setLevel(logging.INFO)


def get_feature(filepath, sr=16000):
    # return mfcc feature as a numpy array with shape (n_mfcc, t)
    # load audio file
    # trim silence
    y, _ = librosa.load(filepath, mono=True, sr=sr)
    yt, idx = librosa.effects.trim(y, top_db=25)

    # extract mfcc features
    # 40 mel-space filters, 25ms hamming window, 10ms shift
    feat = librosa.feature.mfcc(y=yt, sr=sr, n_mfcc=n_mfcc, hop_length=int(sr*0.01), n_fft=int(sr*0.025))

    logger.info("feature obtained, shape (%d, %d)" % (feat.shape(0), feat.shape(1)))
    del y, yt
    return feat


class BaseDataset(Dataset):
    # custom dataset class
    def __init__(self, file_paths, train_mode=False):
        self.file_paths = file_paths
        self.train_mode = train_mode

    def __len__(self):
        # return dataset size
        return len(self.file_paths)

    def __getitem__(self, idx):
        # return loaded numpy array with shape shape (n_mfcc, t)
        feat = get_feature(self.file_paths[idx])
        return feat


def _collate_fn(batch):
    # return batch tensors of input and target
    # batch: a list of numpy arrays with shape (n_mfcc, t) with varying t
    # apply fixed-size sliding window and obtain input-target pairs
    # return tensor shape (batch_size, n_mfcc, n_frames)
    hop_frames = 30
    inputs_list = []
    targets_list = []

    for feat in batch:
        fs = feat.shape[1]

        # only use data with sufficient length
        if fs < 2*n_frames:
            continue

        # number of pairs to obtain from a feature
        hop_num = math.floor((fs - 2*n_frames)/hop_frames)

        for hop in range(hop_num):
            start = hop*hop_frames

            # obtain input-target features
            input_feat = feat[:, start:start + n_frames]
            target_feat = feat[:, start + n_frames:start + 2*n_frames]

            # append to lists
            inputs_list.append(input_feat)
            targets_list.append(target_feat)

    if not inputs_list:
        # no available data after preprocessing
        raise RuntimeError("no available data after preprocessing")

    # make batches
    inputs = np.concatenate(inputs_list, axis=0)
    targets = np.concatenate(targets_list, axis=0)

    inputs = torch.from_numpy(inputs).to(torch.float32)
    targets = torch.from_numpy(targets).to(torch.float32)

    return inputs, targets


class BaseDataLoader(threading.Thread):
    # custom dataloader class
    def __init__(self, dataset, queue, batch_size, thread_id):
        threading.Thread.__init__(self)
        self.collate_fn = _collate_fn
        self.dataset = dataset
        self.queue = queue
        self.index = 0
        self.batch_size = batch_size
        self.dataset_count = len(dataset)
        self.thread_id = thread_id

    def count(self):
        # return number of batches
        return math.ceil(self.dataset_count / self.batch_size)

    def create_empty_batch(self):
        # make empty batches
        inputs = torch.zeros(0, 0, 0).to(torch.float32)
        targets = torch.zeros(0, 0, 0).to(torch.float32)
        return inputs, targets

    def run(self):
        # make batches
        logger.debug('loader %d start' % self.thread_id)

        while True:
            # make a batch as a list of features
            items = list()

            for i in range(self.batch_size):
                if self.index >= self.dataset_count:
                    break
                items.append(self.dataset.getitem(self.index))
                self.index += 1

            # if no features, make empty batches (inputs, targets)
            if len(items) == 0:
                batch = self.create_empty_batch()
                self.queue.put(batch)
                break

            # shuffle features in a batch
            random.shuffle(items)

            # construct batch tensors (inputs, targets)
            batch = self.collate_fn(items)
            self.queue.put(batch)

        logger.debug('loader %d stop' % self.thread_id)


class MultiLoader():
    def __init__(self, dataset_list, queue, batch_size, worker_size):
        self.dataset_list = dataset_list
        self.queue = queue
        self.batch_size = batch_size
        self.worker_size = worker_size
        self.loader = list()

        for i in range(self.worker_size):
            self.loader.append(BaseDataLoader(dataset=self.dataset_list[i],
                                              queue=self.queue,
                                              batch_size=self.batch_size,
                                              thread_id=i))

    def start(self):
        for i in range(self.worker_size):
            self.loader[i].start()

    def join(self):
        for i in range(self.worker_size):
            self.loader[i].join()
