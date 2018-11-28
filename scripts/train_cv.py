#!/usr/bin/env python
"""
Train the ESIM model on some preprocessed dataset.
"""
# Aurelien Coet, 2018.

import numpy as np
import os
import argparse
import time
import pickle
import torch
import json
import matplotlib
matplotlib.use('agg')

import matplotlib.pyplot as plt
import torch.nn as nn
import pickle as pkll
from sklearn.model_selection import KFold
from sklearn.metrics import f1_score

from torch.utils.data import DataLoader
from tqdm import tqdm

try:
    from esim.dataset import NLIDataset
    from esim.model_fusion import ESIM_f
    from esim.utils import correct_predictions
except:
    from dataset import NLIDataset
    from model_fusion import ESIM_f
    from utils import correct_predictions


def train(model,
          dataloader,
          optimizer,
          criterion,
          epoch_number,
          max_gradient_norm, fold):
    """
    Train a model for one epoch on some input data with a given optimizer and
    criterion.

    Args:
        model: A torch module that must be trained on some input data.
        dataloader: A DataLoader object to iterate over the training data.
        optimizer: A torch optimizer to use for training on the input model.
        criterion: A loss criterion to use for training.
        epoch_number: The number of the epoch for which training is performed.
        max_gradient_norm: Max. norm for gradient norm clipping.

    Returns:
        epoch_time: The total time necessary to train the epoch.
        epoch_loss: The training loss computed for the epoch.
        epoch_accuracy: The accuracy computed for the epoch.
    """
    # Switch the model to train mode.
    model.train()
    device = model.device

    epoch_start = time.time()
    batch_time_avg = 0.0
    running_loss = 0.0
    correct_preds = 0

    tqdm_batch_iterator = tqdm(dataloader)
    for batch_index, batch in enumerate(tqdm_batch_iterator):
        batch_start = time.time()

        # Move input and output data to the GPU if it is used.
        premises = batch['premise'].to(device)
        premises_lengths = batch['premise_length'].to(device)
        hypotheses = batch['hypothesis'].to(device)
        hypotheses_lengths = batch['hypothesis_length'].to(device)
        labels = batch['label'].to(device)

        optimizer.zero_grad()

        logits, probs = model(premises,
                              premises_lengths,
                              hypotheses,
                              hypotheses_lengths)
        loss = criterion(logits, labels)
        loss.backward()

        nn.utils.clip_grad_norm_(model.parameters(), max_gradient_norm)
        optimizer.step()

        batch_time_avg += time.time() - batch_start
        running_loss += loss.item()
        correct_preds += correct_predictions(probs, labels)

        description = "Avg. kfold={}, batch proc. time: {:.4f}s, loss: {:.4f}"\
                      .format(fold, batch_time_avg/(batch_index+1),
                              running_loss/(batch_index+1))
        tqdm_batch_iterator.set_description(description)

    epoch_time = time.time() - epoch_start
    epoch_loss = running_loss / len(dataloader)
    epoch_accuracy = correct_preds / len(dataloader.dataset)

    return epoch_time, epoch_loss, epoch_accuracy


def validate(model, dataloader, criterion):
    """
    Compute the loss and accuracy of a model on some validation dataset.

    Args:
        model: A torch module for which the loss and accuracy must be
            computed.
        dataloader: A DataLoader object to iterate over the validation data.
        criterion: A loss criterion to use for computing the loss.
        epoch: The number of the epoch for which validation is performed.
        device: The device on which the model is located.

    Returns:
        epoch_time: The total time to compute the loss and accuracy on the
            entire validation set.
        epoch_loss: The loss computed on the entire validation set.
        epoch_accuracy: The accuracy computed on the entire validation set.
    """
    # Switch to evaluate mode.
    model.eval()
    device = model.device

    epoch_start = time.time()
    running_loss = 0.0
    running_accuracy = 0.0
    true_label = []
    prediction = []

    # Deactivate autograd for evaluation.
    with torch.no_grad():
        for batch in dataloader:
            # Move input and output data to the GPU if one is used.
            premises = batch['premise'].to(device)
            premises_lengths = batch['premise_length'].to(device)
            hypotheses = batch['hypothesis'].to(device)
            hypotheses_lengths = batch['hypothesis_length'].to(device)
            labels = batch['label'].to(device)

            logits, probs = model(premises,
                                  premises_lengths,
                                  hypotheses,
                                  hypotheses_lengths)
            loss = criterion(logits, labels)

            running_loss += loss.item()
            running_accuracy += correct_predictions(probs, labels)
            prediction += [int(w) for w in torch.argmax(probs, 1).data]
            true_label += [int(w) for w in labels.data]

    epoch_time = time.time() - epoch_start
    epoch_loss = running_loss / len(dataloader)
    epoch_accuracy = running_accuracy / (len(dataloader.dataset))
    f1_ = f1_score(true_label, prediction)

    return epoch_time, epoch_loss, epoch_accuracy, f1_


def main(train_data,
         valid_data,
         embeddings_file,
         target_dir,
         hidden_size=300,
         dropout=0.5,
         num_classes=3,
         epochs=64,
         batch_size=32,
         patience=5,
         max_grad_norm=10.0,
         checkpoint=None,
         fold=0):
    """
    Train the ESIM model on some dataset.

    Args:
        train_file: A path to some preprocessed data that must be used
            to train the model.
        valid_file: A path to some preprocessed data that must be used
            to validate the model.
        embeddings_file: A path to some preprocessed word embeddings that
            must be used to initialise the model.
        target_dir: The path to a directory where the trained model must
            be saved.
        hidden_size: The size of the hidden layers in the model. Defaults
            to 300.
        dropout: The dropout rate to use in the model. Defaults to 0.5.
        num_classes: The number of classes in the output of the model.
            Defaults to 3.
        epochs: The maximum number of epochs for training. Defaults to 64.
        batch_size: The size of the batches for training. Defaults to 32.
        patience: The patience to use for early stopping. Defaults to 5.
        checkpoint: A checkpoint from which to continue training. If None,
            training starts from scratch. Defaults to None.
    """
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    print(20 * "=", " Preparing for training ", 20 * "=")

    if not os.path.exists(target_dir):
        os.makedirs(target_dir)

    # -------------------- Data loading ------------------- #
    print("\t* Loading training data...")
    train_data = NLIDataset(train_data)

    train_loader = DataLoader(train_data, shuffle=True, batch_size=batch_size)

    print("\t* Loading validation data...")
    valid_data = NLIDataset(valid_data)

    valid_loader = DataLoader(valid_data, shuffle=False, batch_size=batch_size)
    word_dict = pkll.load(open('../data/preprocessed/worddict.pkl', 'rb'))



    # -------------------- Model definition ------------------- #
    print('\t* Building model...')
    with open(embeddings_file, 'rb') as pkl:
        embeddings = torch.tensor(pickle.load(pkl), dtype=torch.float)\
                     .to(device)

    model = ESIM_f(embeddings.shape[0],
                 embeddings.shape[1],
                 hidden_size,
                 embeddings=embeddings,
                 dropout=dropout,
                 num_classes=num_classes,
                 device=device).to(device)

    # -------------------- Preparation for training  ------------------- #
    criterion = nn.CrossEntropyLoss()
    param = filter(lambda p: p.requires_grad, model.parameters())
    optimizer = torch.optim.Adam(param, lr=0.0004)
    #optimizer = torch.optim.Adam(model.parameters(), lr=0.0004)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer,
                                                           mode='max',
                                                           factor=0.5,
                                                           patience=0)

    best_score = 0.0
    start_epoch = 1

    # Data for loss curves plot.
    epochs_count = []
    train_losses = []
    valid_losses = []

    # Continuing training from a checkpoint if one was given as argument.
    if checkpoint:
        checkpoint = torch.load(checkpoint)
        start_epoch = checkpoint['epoch'] + 1
        best_score = checkpoint['best_score']

        print("\t* Training will continue on existing model from epoch {}..."
              .format(start_epoch))

        model.load_state_dict(checkpoint['model'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        epochs_count = checkpoint['epochs_count']
        train_losses = checkpoint['train_losses']
        valid_losses = checkpoint['valid_losses']

    # Compute loss and accuracy before starting (or resuming) training.
    _, valid_loss, valid_accuracy, f1_ = validate(model,
                                             valid_loader,
                                             criterion)
    print("\t* Validation loss before training: {:.4f}, accuracy: {:.4f}%, f1: {:.4f}%\n"
          .format(valid_loss, (valid_accuracy*100), (f1_*100)))

    # -------------------- Training epochs ------------------- #
    print("\n",
          20 * "=",
          "Training ESIM model on device: {}".format(device),
          20 * "=")

    patience_counter = 0
    for epoch in range(start_epoch, epochs+1):
        epochs_count.append(epoch)

        print("* Training epoch {}:".format(epoch))
        epoch_time, epoch_loss, epoch_accuracy = train(model,
                                                       train_loader,
                                                       optimizer,
                                                       criterion,
                                                       epoch,
                                                       max_grad_norm,
                                                       fold)

        train_losses.append(epoch_loss)
        print("-> Training time: {:.4f}s, loss = {:.4f}, accuracy: {:.4f}%"
              .format(epoch_time, epoch_loss, (epoch_accuracy*100)))

        print("* Validation for epoch {}:".format(epoch))
        epoch_time, epoch_loss, epoch_accuracy, epoch_f1 = validate(model,
                                                          valid_loader,
                                                          criterion)

        valid_losses.append(epoch_loss)
        print("-> Valid. time: {:.4f}s, loss: {:.4f}, accuracy: {:.4f}%, f1: {:.4f}%\n"
              .format(epoch_time, epoch_loss, (epoch_accuracy*100), (epoch_f1*100)))

        # Update the optimizer's learning rate with the scheduler.
        #scheduler.step(epoch_accuracy)
        scheduler.step(epoch_f1)

        # Early stopping on validation accuracy.
        if epoch_f1 < best_score:
            patience_counter += 1
        else:
            best_score = epoch_f1
            patience_counter = 0
            # Save the best model. The optimizer is not saved to avoid having
            # a checkpoint file that is too heavy to be shared. To resume
            # training from the best model, use the 'esim_*.pth.tar'
            # checkpoints instead.
            torch.save({'epoch': epoch,
                        'model': model.state_dict(),
                        'best_score': best_score,
                        'epochs_count': epochs_count,
                        'train_losses': train_losses,
                        'valid_losses': valid_losses},
                       os.path.join(target_dir, "best_{}.pth.tar".format(fold)))

        # Save the model at each epoch.
        torch.save({'epoch': epoch,
                    'model': model.state_dict(),
                    'best_score': best_score,
                    'optimizer': optimizer.state_dict(),
                    'epochs_count': epochs_count,
                    'train_losses': train_losses,
                    'valid_losses': valid_losses},
                   os.path.join(target_dir, "esim_{}.pth.tar".format(epoch)))

        if patience_counter >= patience:
            print("-> Early stopping: patience limit reached, stopping...")
            break

    # Plotting of the loss curves for the train and validation sets.
    plt.figure()
    plt.plot(epochs_count, train_losses, '-r')
    plt.plot(epochs_count, valid_losses, '-b')
    plt.xlabel('epoch')
    plt.ylabel('loss')
    plt.legend(['Training loss', 'Validation loss'])
    plt.title('Cross entropy loss')
    plt.show()
    return best_score

def cross_validation(train_file,
             valid_file,
             embeddings_file,
             target_dir,
             hidden_size=300,
             dropout=0.5,
             num_classes=3,
             epochs=64,
             batch_size=32,
             patience=5,
             max_grad_norm=10.0,
             checkpoint=None,
                fold=5):
    train_data = pickle.load(open(train_file, 'rb'))
    kf = KFold(n_splits=fold)
    keys = [k for k in train_data]
    scores = []
    for fold, (train_index, test_index) in enumerate(kf.split(train_data['labels'])):
        print(20*"=", "Fold {} start....".format(fold), "="*20, "\n"*2)
        train_fold = {k:[train_data[k][w] for w in train_index] for k in keys}
        test_fold = {k:[train_data[k][w] for w in test_index] for k in keys}
        score = main(train_fold, test_fold, embeddings_file, target_dir, hidden_size=hidden_size, dropout=dropout,
             num_classes=num_classes, epochs=epochs, batch_size=batch_size, patience=patience,
             max_grad_norm=max_grad_norm, checkpoint=checkpoint, fold=fold)
        scores.append(score)
        print(20*"=", "Fold {} end...., score: {:.4f}%".format(fold, score * 100), "="*20)
    print("\t"*4, "Final score: {:.4f}%".format(np.mean(scores*100)), "\t"*4, "\n"*2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Train the ESIM model')
    parser.add_argument('--config',
                        default="../config/train.json",
                        help='Path to a json configuration file')
    parser.add_argument('--checkpoint',
                        default=None,
                        help='path to a checkpoint file to resume training')
    args = parser.parse_args()

    with open(os.path.normpath(args.config), 'r') as config_file:
        config = json.load(config_file)

    cross_validation(os.path.normpath(config["train_data"]),
         os.path.normpath(config["valid_data"]),
         os.path.normpath(config["embeddings"]),
         os.path.normpath(config["target_dir"]),
         config["hidden_size"],
         config["dropout"],
         config["num_classes"],
         config["epochs"],
         config["batch_size"],
         config["patience"],
         config["max_gradient_norm"],
         args.checkpoint,
         config['kfold'])
