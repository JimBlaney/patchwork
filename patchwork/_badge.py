# -*- coding: utf-8 -*-
"""

            _badge.py

Support code for the BADGE active learning algorithm. See DEEP BATCH ACTIVE 
LEARNING BY DIVERSE, UNCERTAIN GRADIENT LOWER BOUNDS by Ash et al
"""
import numpy as np
import tensorflow as tf
from scipy.spatial.distance import cdist

class KPlusPlusSampler():
    """
    Class for drawing random indices using the initialization
    algorithm from kmeans++
    """
    def __init__(self, X, indices=None):
        """
        :X: (N,d) array of vector
        :indices: initial list of indices (for example, previously-
            labeled records)
        """
        self.X = X
        self.N = X.shape[0]
        self.d = X.shape[1]
        if indices is None:
            indices = []
            
        self.indices = indices
        
        if len(indices) > 0:
            self.min_dists = cdist(X[np.array(indices),:], X).min(axis=0)
        
    def _choose_initial_index(self):
        ind = np.random.randint(self.N)
        self.indices.append(ind)
        self.min_dists = cdist(self.X[np.array([ind]),:], self.X).min(axis=0)
        return ind
    
    def _choose_non_initial_index(self):
        # compute sampling probabilities
        p = self.min_dists**2
        p /= p.sum()
        # sample new index
        ind = np.random.choice(np.arange(self.N), p=p)
        self.indices.append(ind)
        # update min distances
        min_dists = cdist(self.X[np.array([ind]),:], self.X).min(axis=0)
        self.min_dists = np.minimum(self.min_dists, min_dists)
        return ind
    
    def choose(self, k=1):
        """
        Return a list of k sample indices
        """
        indices = []
        for _ in range(k):
            if len(self.indices) == 0:
                ind = self._choose_initial_index()
            else:
                ind = self._choose_non_initial_index()
            indices.append(ind)
            
        return indices
    
    def __call__(self, k=1):
        """
        Return a list of k samples
        """
        return self.choose(k)
        


def _build_output_gradient_function(fine_tuning_model, output_model, feature_extractor=None):
    """
    Generate a tensorflow function for computing, for a given example, the gradient of
    the loss function with respect to the weights in the final layer. This is useful
    for active learning- see "DEEP BATCH ACTIVE LEARNING BY DIVERSE, UNCERTAIN GRADIENT 
    LOWER BOUNDS" by Ash et al.
    
    """
    # THERE SHOULD ONLY BE ONE TENSOR IN THIS LIST
    output_weights = [x for x in output_model.trainable_variables if "kernel" in x.name][0]
    
    @tf.function
    def compute_output_gradients(x):
        # since we're working one record at a time- add a
        # batch dimension
        x = tf.expand_dims(x,0)
        # if we're not using pre-extracted feature tensors
        if feature_extractor is not None:
            x = feature_extractor(x)
        # push feature tensor through fine-tuning model to get a vector
        x = fine_tuning_model(x)
        # push vectors through output model and round predictions 
        # to make pseudolabels
        label = tf.cast(output_model(x) >= 0.5, tf.float32)
        # now compute a gradient of the loss function against
        # pseudolabels with respect to the output model weights
        with tf.GradientTape() as tape:
            pred = output_model(x)
            loss = tf.keras.losses.binary_crossentropy(label, pred)

        grad = tape.gradient(loss, output_weights)
        # finally flatten the gradient tensor back to a vector
        return tf.reshape(grad, [-1])
    return compute_output_gradients