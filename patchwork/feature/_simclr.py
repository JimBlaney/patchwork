# -*- coding: utf-8 -*-
import numpy as np
import tensorflow as tf

from patchwork.feature._generic import GenericExtractor
from patchwork._augment import augment_function
from patchwork.loaders import _image_file_dataset

BIG_NUMBER = 1000.

def build_simclr_dataset(imfiles, imshape=(256,256), batch_size=256, 
                      num_parallel_calls=None, norm=255,
                      num_channels=3, augment=True,
                      single_channel=False):
    """
    
    """
    assert augment, "don't you need to augment your data?"
    _aug = augment_function(imshape, augment)
    
    ds = _image_file_dataset(imfiles, imshape=imshape, 
                             num_parallel_calls=num_parallel_calls,
                             norm=norm, num_channels=num_channels,
                             shuffle=True, single_channel=single_channel)  
    @tf.function
    def _augment_and_stack(x):
        y = tf.constant(np.array([1,-1]).astype(np.int32))
        return tf.stack([_aug(x),_aug(x)]), y

    ds = ds.map(_augment_and_stack, num_parallel_calls=num_parallel_calls)
    
    ds = ds.unbatch()
    ds = ds.batch(batch_size, drop_remainder=True)
    ds = ds.prefetch(1)
    return ds


def _build_embedding_model(fcn, imshape, num_channels, num_hidden, output_dim):
    """
    Create a Keras model that wraps the base encoder and 
    the projection head
    """
    inpt = tf.keras.layers.Input((imshape[0], imshape[1], num_channels))
    net = fcn(inpt)
    net = tf.keras.layers.Flatten()(net)
    net = tf.keras.layers.Dense(num_hidden, activation="relu")(net)
    net = tf.keras.layers.Dense(output_dim)(net)
    embedding_model = tf.keras.Model(inpt, net)
    return embedding_model





def build_simclr_training_step(embed_model, optimizer, temperature=0.1):
    """
    
    """
    @tf.function
    def training_step(x,y):
        eye = tf.linalg.eye(x.shape[0])
        index = tf.range(0, x.shape[0])

        with tf.GradientTape() as tape:
            # run each image through the convnet and
            # projection head
            embeddings = embed_model(x, training=True)
            # normalize the embeddings
            embeds_norm = tf.nn.l2_normalize(embeddings)
            # compute the pairwise matrix of cosine similarities
            sim = tf.matmul(embeds_norm, embeds_norm, transpose_b=True)
            # subtract a large number from diagonals to effectively remove
            # them from the sum, and rescale by temperature
            logits = (sim - BIG_NUMBER*eye)/temperature
            # the labels tell which similarity is the "correct" one- the augmented
            # pair from the same image. so index+y should look like [1,0,3,2,5,4...]
            labels = index+y
            loss = tf.reduce_mean(
                    tf.nn.sparse_softmax_cross_entropy_with_logits(labels, logits))

        gradients = tape.gradient(loss, embed_model.trainable_variables)
        optimizer.apply_gradients(zip(gradients,
                                      embed_model.trainable_variables))
        return loss






class SimCLRTrainer(GenericExtractor):
    """
    Class for training a SimCLR model.
    
    Based on "A Simple Framework for Contrastive Learning of Visual
    Representations" by Chen et al.
    """

    def __init__(self, logdir, trainingdata, testdata=None, fcn=None, 
                 augment=True, temperature=1., num_hidden=256,
                 output_dim=64,
                 lr=0.01, lr_decay=100000,
                 imshape=(256,256), num_channels=3,
                 norm=255, batch_size=64, num_parallel_calls=None,
                 sobel=False, single_channel=False, notes="",
                 downstream_labels=None):
        """
        :logdir: (string) path to log directory
        :trainingdata: (list) list of paths to training images
        :testdata: (list) filepaths of a batch of images to use for eval
        :fcn: (keras Model) fully-convolutional network to train as feature extractor
        :augment: (dict) dictionary of augmentation parameters, True for defaults
        :temperature:
        :num_hidden:
        :output_dim:
        :lr: (float) initial learning rate
        :lr_decay: (int) steps for learning rate to decay by half (0 to disable)
        :imshape: (tuple) image dimensions in H,W
        :num_channels: (int) number of image channels
        :norm: (int or float) normalization constant for images (for rescaling to
               unit interval)
        :batch_size: (int) batch size for training
        :num_parallel_calls: (int) number of threads for loader mapping
        :sobel: whether to replace the input image with its sobel edges
        :single_channel: if True, expect a single-channel input image and 
                stack it num_channels times.
        :notes: (string) any notes on the experiment that you want saved in the
                config.yml file
        :downstream_labels: dictionary mapping image file paths to labels
        """
        assert augment is not False, "this method needs an augmentation scheme"
        self.logdir = logdir
        self.trainingdata = trainingdata
        self._downstream_labels = downstream_labels
        channels = 3 if sobel else num_channels
        
        self._file_writer = tf.summary.create_file_writer(logdir, flush_millis=10000)
        self._file_writer.set_as_default()
        
        # if no FCN is passed- build one
        if fcn is None:
            fcn = tf.keras.applications.ResNet50V2(weights=None, include_top=False)
        self.fcn = fcn
        # Create a Keras model that wraps the base encoder and 
        # the projection head
        embed_model = _build_embedding_model(fcn, imshape, num_channels,
                                             num_hidden, output_dim)
        
        self._models = {"fcn":fcn, 
                        "full":embed_model}
        
        # build training dataset
        self._ds = build_simclr_dataset(trainingdata, 
                                        imshape=imshape, batch_size=batch_size,
                                        num_parallel_calls=num_parallel_calls, 
                                        norm=norm, num_channels=num_channels, 
                                        augment=augment,
                                        single_channel=single_channel)
        
        # create optimizer
        if lr_decay > 0:
            learnrate = tf.keras.optimizers.schedules.ExponentialDecay(lr, 
                                            decay_steps=lr_decay, decay_rate=0.5,
                                            staircase=False)
        else:
            learnrate = lr
        self._optimizer = tf.keras.optimizers.SGD(learnrate, momentum=0.9)
        
        
        # build training step
        self._training_step = build_simclr_training_step(
                embed_model, 
                self._optimizer, 
                temperature)
        
        self._test = False
        self._test_labels = None
        self._old_test_labels = None
        
        self.step = 0
        
        # parse and write out config YAML
        self._parse_configs(augment=augment, temperature=temperature,
                            num_hidden=num_hidden, output_dim=output_dim,
                            lr=lr, lr_decay=lr_decay, 
                            imshape=imshape, num_channels=num_channels,
                            norm=norm, batch_size=batch_size,
                            num_parallel_calls=num_parallel_calls, sobel=sobel,
                            single_channel=single_channel, notes=notes)

    def _run_training_epoch(self, **kwargs):
        """
        
        """
        for x, y in self._ds:
            loss = self._training_step(x,y)
            
            self._record_scalars(loss=loss)
            self.step += 1
            
 
    def evaluate(self):
        if self._downstream_labels is not None:
            # choose the hyperparameters to record
            if not hasattr(self, "_hparams_config"):
                from tensorboard.plugins.hparams import api as hp
                hparams = {
                    hp.HParam("temperature", hp.RealInterval(0., 10000.)):self.config["temperature"],
                    hp.HParam("num_hidden", hp.IntInterval(1, 1000000)):self.config["num_hidden"],
                    hp.HParam("output_dim", hp.IntInterval(1, 1000000)):self.config["output_dim"],
                    hp.HParam("sobel", hp.Discrete([True, False])):self.input_config["sobel"]
                    }
            else:
                hparams=None
            self._linear_classification_test(hparams)
        
