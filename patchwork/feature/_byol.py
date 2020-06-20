# -*- coding: utf-8 -*-
"""

    Implementation of BYOL from "Bootstrap Your Own Latent: A New Approach to 
    Self-Supervised Learning" by Grill et al.


"""
import numpy as np
import tensorflow as tf
from tqdm import tqdm

from patchwork.feature._generic import GenericExtractor
from patchwork.feature._moco import exponential_model_update

from patchwork._augment import augment_function
from patchwork.loaders import _image_file_dataset
from patchwork._util import compute_l2_loss


#BIG_NUMBER = 1000.

def _perceptron(input_dim, num_hidden=4096, output_dim=256, batchnorm=True):
    # macro to build a single-hidden-layer perceptron
    inpt = tf.keras.layers.Input((input_dim,))
    net = tf.keras.layers.Dense(num_hidden, activation="relu")(inpt)
    if batchnorm:
        net = tf.keras.layers.BatchNormalization()(net)
    net = tf.keras.layers.Dense(output_dim)(net)
    return tf.keras.Model(inpt, net)

def _build_models(fcn, imshape, num_channels, num_hidden=4096, output_dim=256):
    """
    Initialize all the model components we'll need for training
    """
    #  --- ONLINE MODEL ---
    # input view
    inpt = tf.keras.layers.Input((imshape[0], imshape[1], num_channels))
    # representation
    rep_tensor = fcn(inpt)
    rep = tf.keras.layers.Flatten()(rep_tensor)
    rep_dimension = rep.shape[-1]
    # projection
    online_projector = _perceptron(rep_dimension, num_hidden, output_dim)
    proj = online_projector(rep)
    online = tf.keras.Model(inpt, proj)
    # prediction
    online_predictor = _perceptron(output_dim, num_hidden, output_dim)
    #pred = online_predictor(proj)
    
    #  --- TARGET MODEL ---
    target = tf.keras.models.clone_model(online)
    
    return {"fcn":fcn, "online":online, "prediction":online_predictor,
            "target":target}


def _build_byol_dataset(imfiles, imshape=(256,256), batch_size=256, 
                      num_parallel_calls=None, norm=255,
                      num_channels=3, augment=True,
                      single_channel=False):
    """
    :stratify: if not None, a list of categories for each element in
        imfile.
    """
    assert augment != False, "don't you need to augment your data?"
    
    ds = _image_file_dataset(imfiles, imshape=imshape, 
                             num_parallel_calls=num_parallel_calls,
                             norm=norm, num_channels=num_channels,
                             shuffle=True, single_channel=single_channel,
                             augment=False)  
    
    _aug = augment_function(imshape, augment)
    @tf.function
    def pair_augment(x):
        return (_aug(x), _aug(x)), np.array([1])
    
    ds = ds.map(pair_augment, num_parallel_calls=num_parallel_calls)
    
    ds = ds.batch(batch_size)
    ds = ds.prefetch(1)
    return ds


def _norm(x):
    # L2-normalizing wrapper
    return tf.nn.l2_normalize(x, axis=1)

def _mse(y_true, y_pred):
    # mean-squared error loss wrapper
    return y_true.shape[-1]*tf.reduce_mean(
        tf.keras.losses.mean_squared_error(y_true, y_pred))

def _build_byol_training_step(online, prediction, target, optimizer,
                              tau, weight_decay=0):
    """
    
    """
    trainvars = online.trainable_variables + prediction.trainable_variables
    
    def training_step(x,y):
        x1, x2 = x
        lossdict = {}
        
        # target projections
        targ1 = _norm(target(x1))
        targ2 = _norm(target(x2))
        
        with tf.GradientTape() as tape:
            # online projections
            z1 = online(x1, training=True)
            z2 = online(x2, training=True)
            # online predictions
            pred1 = _norm(prediction(z1))
            pred2 = _norm(prediction(z2))
            # compute mean-squared error both ways
            mse_loss = _mse(targ1, pred2) + _mse(targ2, pred1)
            lossdict["loss"] = mse_loss
            lossdict["mse_loss"] = mse_loss

            if weight_decay > 0:
                lossdict["l2_loss"] = compute_l2_loss(online) + \
                            compute_l2_loss(prediction)
                lossdict["loss"] += weight_decay*lossdict["l2_loss"]
           
        # UPDATE WEIGHTS OF ONLINE MODEL
        gradients = tape.gradient(lossdict["loss"], trainvars)
        optimizer.apply_gradients(zip(gradients, trainvars))
        # UPDATE WEIGHTS OF TARGET MODEL
        exponential_model_update(target, online, tau)
        
        return lossdict
    return training_step
        
        


class BYOLTrainer(GenericExtractor):
    """
    Class for training a BYOL model.
    
    Based on  "Bootstrap Your Own Latent: A New Approach to 
    Self-Supervised Learning" by Grill et al.
    """

    def __init__(self, logdir, trainingdata, testdata=None, fcn=None, 
                 augment=True, num_hidden=4096, output_dim=256,
                 tau=0.996, weight_decay=0,
                 lr=0.01, lr_decay=100000, decay_type="exponential",
                 imshape=(256,256), num_channels=3,
                 norm=255, batch_size=64, num_parallel_calls=None,
                 single_channel=False, notes="",
                 downstream_labels=None, strategy=None):
        """
        :logdir: (string) path to log directory
        :trainingdata: (list) list of paths to training images
        :testdata: (list) filepaths of a batch of images to use for eval
        :fcn: (keras Model) fully-convolutional network to train as feature extractor
        :augment: (dict) dictionary of augmentation parameters, True for defaults
        :num_hidden:
        :output_dim:
        :weight_decay: coefficient for L2-norm loss. The original SimCLR paper used 1e-6.
        :lr: (float) initial learning rate
        :lr_decay: (int) steps for learning rate to decay by half (0 to disable)
        :decay_type: (str) how to decay learning rate; "exponential" or "cosine"
        :imshape: (tuple) image dimensions in H,W
        :num_channels: (int) number of image channels
        :norm: (int or float) normalization constant for images (for rescaling to
               unit interval)
        :batch_size: (int) batch size for training
        :num_parallel_calls: (int) number of threads for loader mapping
        :single_channel: if True, expect a single-channel input image and 
                stack it num_channels times.
        :notes: (string) any notes on the experiment that you want saved in the
                config.yml file
        :downstream_labels: dictionary mapping image file paths to labels
        :strategy: if distributing across multiple GPUs, pass a tf.distribute
            Strategy object here. NOT YET IMPLEMENTED
        """
        assert augment is not False, "this method needs an augmentation scheme"
        self.logdir = logdir
        self.trainingdata = trainingdata
        self._downstream_labels = downstream_labels
        self.strategy = strategy
        
        self._file_writer = tf.summary.create_file_writer(logdir, flush_millis=10000)
        self._file_writer.set_as_default()
        
        # if no FCN is passed- build one
        with self.scope():
            if fcn is None:
                fcn = tf.keras.applications.ResNet50V2(weights=None, include_top=False)
            self.fcn = fcn
            # Create Keras models for the full online model, online predictions,
            # and target model
            self._models = _build_models(fcn, imshape, num_channels)
            
        # build training dataset
        ds = _build_byol_dataset(trainingdata, 
                                   imshape=imshape, batch_size=batch_size,
                                   num_parallel_calls=num_parallel_calls, 
                                   norm=norm, num_channels=num_channels, 
                                   augment=augment,
                                   single_channel=single_channel)
        self._ds = self._distribute_dataset(ds)
        
        # create optimizer
        self._optimizer = self._build_optimizer(lr, lr_decay,
                                                decay_type=decay_type)
        
        
        # build training step
        step_fn = _build_byol_training_step(
                                    self._models["online"],
                                    self._models["prediction"],
                                    self._models["target"],
                                    self._optimizer,
                                    tau, weight_decay)
        self._training_step = self._distribute_training_function(step_fn)
        
        if testdata is not None:
            """
            self._test_ds = _build_byol_dataset(testdata, 
                                        imshape=imshape, batch_size=batch_size,
                                        num_parallel_calls=num_parallel_calls, 
                                        norm=norm, num_channels=num_channels, 
                                        augment=augment,
                                        single_channel=single_channel)
            
            @tf.function
            def test_loss(x,y):
                eye = tf.linalg.eye(y.shape[0])
                index = tf.range(0, y.shape[0])
                labels = index+y

                embeddings = self._models["full"](x)
                embeds_norm = tf.nn.l2_normalize(embeddings, axis=1)
                sim = tf.matmul(embeds_norm, embeds_norm, transpose_b=True)
                logits = (sim - BIG_NUMBER*eye)/self.config["temperature"]
            
                loss = tf.reduce_mean(
                    tf.nn.sparse_softmax_cross_entropy_with_logits(labels, logits))
                return loss, sim
            self._test_loss = test_loss
            """
            self._test = True
        else:
            self._test = False
        
        self.step = 0
        
        # parse and write out config YAML
        self._parse_configs(augment=augment,
                            num_hidden=num_hidden, output_dim=output_dim,
                            weight_decay=weight_decay, tau=tau,
                            lr=lr, lr_decay=lr_decay, 
                            imshape=imshape, num_channels=num_channels,
                            norm=norm, batch_size=batch_size,
                            num_parallel_calls=num_parallel_calls,
                            single_channel=single_channel, notes=notes,
                            trainer="byol", strategy=str(strategy),
                            decay_type=decay_type)

    def _run_training_epoch(self, **kwargs):
        """
        
        """
        for x, y in self._ds:
            lossdict = self._training_step(x,y)
            self._record_scalars(**lossdict)
            self._record_scalars(learning_rate=self._get_current_learning_rate())
            self.step += 1
             
    def evaluate(self):
        if self._test:
            pass
            """
            for x,y in self._test_ds:
                loss, sim = self._test_loss(x,y)
                test_loss += loss.numpy()
                
            self._record_scalars(test_loss=test_loss)
            # I'm commenting out this tensorboard image- takes up a lot of
            # space but doesn't seem to add much
            #self._record_images(scalar_products=tf.expand_dims(tf.expand_dims(sim,-1), 0))
            """
        if self._downstream_labels is not None:
            # choose the hyperparameters to record
            if not hasattr(self, "_hparams_config"):
                from tensorboard.plugins.hparams import api as hp
                hparams = {
                    hp.HParam("tau", hp.RealInterval(0., 1.)):self.config["tau"],
                    hp.HParam("num_hidden", hp.IntInterval(1, 1000000)):self.config["num_hidden"],
                    hp.HParam("output_dim", hp.IntInterval(1, 1000000)):self.config["output_dim"],
                    hp.HParam("lr", hp.RealInterval(0., 10000.)):self.config["lr"],
                    hp.HParam("lr_decay", hp.RealInterval(0., 10000.)):self.config["lr_decay"],
                    hp.HParam("decay_type", hp.Discrete(["cosine", "exponential"])):self.config["decay_type"],
                    hp.HParam("weight_decay", hp.RealInterval(0., 10000.)):self.config["weight_decay"]
                    }
                for k in self.augment_config:
                    if isinstance(self.augment_config[k], float):
                        hparams[hp.HParam(k, hp.RealInterval(0., 10000.))] = self.augment_config[k]
            else:
                hparams=None
            self._linear_classification_test(hparams)
        