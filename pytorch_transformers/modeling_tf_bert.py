# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" TF 2.0 BERT model. """

from __future__ import absolute_import, division, print_function, unicode_literals

import json
import logging
import math
import os
import sys
from io import open

import numpy as np
import tensorflow as tf

from .configuration_bert import BertConfig
from .modeling_tf_utils import TFPreTrainedModel
from .file_utils import add_start_docstrings

logger = logging.getLogger(__name__)


TF_BERT_PRETRAINED_MODEL_ARCHIVE_MAP = {
    'bert-base-uncased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-base-uncased-tf_model.h5",
    'bert-large-uncased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-uncased-tf_model.h5",
    'bert-base-cased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-base-cased-tf_model.h5",
    'bert-large-cased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-cased-tf_model.h5",
    'bert-base-multilingual-uncased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-base-multilingual-uncased-tf_model.h5",
    'bert-base-multilingual-cased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-base-multilingual-cased-tf_model.h5",
    'bert-base-chinese': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-base-chinese-tf_model.h5",
    'bert-base-german-cased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-base-german-cased-tf_model.h5",
    'bert-large-uncased-whole-word-masking': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-uncased-whole-word-masking-tf_model.h5",
    'bert-large-cased-whole-word-masking': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-cased-whole-word-masking-tf_model.h5",
    'bert-large-uncased-whole-word-masking-finetuned-squad': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-uncased-whole-word-masking-finetuned-squad-tf_model.h5",
    'bert-large-cased-whole-word-masking-finetuned-squad': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-cased-whole-word-masking-finetuned-squad-tf_model.h5",
    'bert-base-cased-finetuned-mrpc': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-base-cased-finetuned-mrpc-tf_model.h5",
}


def load_pt_weights_in_bert(tf_model, config, pytorch_checkpoint_path):
    """ Load pytorch checkpoints in a TF 2.0 model and save it using HDF5 format
        We use HDF5 to easily do transfer learning
        (see https://github.com/tensorflow/tensorflow/blob/ee16fcac960ae660e0e4496658a366e2f745e1f0/tensorflow/python/keras/engine/network.py#L1352-L1357).
    """
    try:
        import re
        import torch
        import numpy
        from tensorflow.python.keras import backend as K
    except ImportError:
        logger.error("Loading a PyTorch model in TensorFlow, requires PyTorch to be installed. Please see "
            "https://pytorch.org/ for installation instructions.")
        raise

    pt_path = os.path.abspath(pytorch_checkpoint_path)
    logger.info("Loading PyTorch weights from {}".format(pt_path))
    # Load pytorch model
    state_dict = torch.load(pt_path, map_location='cpu')

    inputs_list = [[7, 6, 0, 0, 1], [1, 2, 3, 0, 0], [0, 0, 0, 4, 5]]
    tf_inputs = tf.constant(inputs_list)
    tfo = tf_model(tf_inputs, training=False)  # build the network

    symbolic_weights = tf_model.trainable_weights + tf_model.non_trainable_weights
    weight_value_tuples = []
    for symbolic_weight in symbolic_weights:
        name = symbolic_weight.name
        name = name.replace('cls_mlm', 'cls')  # We had to split this layer in two in the TF model to be
        name = name.replace('cls_nsp', 'cls')  # able to do transfer learning (Keras only allow to remove full layers)
        name = name.replace(':0', '')
        name = name.replace('layer_', 'layer/')
        name = name.split('/')
        name = name[1:]

        transpose = bool(name[-1] == 'kernel')
        if name[-1] == 'kernel' or name[-1] == 'embeddings':
            name[-1] = 'weight'

        name = '.'.join(name)
        assert name in state_dict
        array = state_dict[name].numpy()

        if transpose:
            array = numpy.transpose(array)

        try:
            assert list(symbolic_weight.shape) == list(array.shape)
        except AssertionError as e:
            e.args += (symbolic_weight.shape, array.shape)
            raise e

        logger.info("Initialize TF weight {}".format(symbolic_weight.name))

        weight_value_tuples.append((symbolic_weight, array))

    K.batch_set_value(weight_value_tuples)

    tfo = tf_model(tf_inputs, training=False)  # Make sure restore ops are run
    return tf_model


def gelu(x):
    """Gaussian Error Linear Unit.
    This is a smoother version of the RELU.
    Original paper: https://arxiv.org/abs/1606.08415
    Args:
        x: float Tensor to perform activation.
    Returns:
        `x` with the GELU activation applied.
    """
    cdf = 0.5 * (1.0 + tf.tanh(
        (np.sqrt(2 / np.pi) * (x + 0.044715 * tf.pow(x, 3)))))
    return x * cdf


def swish(x):
    return x * tf.sigmoid(x)


ACT2FN = {"gelu": tf.keras.layers.Activation(gelu),
          "relu": tf.keras.activations.relu,
          "swish": tf.keras.layers.Activation(swish)}


class TFBertEmbeddings(tf.keras.layers.Layer):
    """Construct the embeddings from word, position and token_type embeddings.
    """
    def __init__(self, config, **kwargs):
        super(TFBertEmbeddings, self).__init__(**kwargs)
        self.word_embeddings = tf.keras.layers.Embedding(config.vocab_size, config.hidden_size, name='word_embeddings')
        self.position_embeddings = tf.keras.layers.Embedding(config.max_position_embeddings, config.hidden_size, name='position_embeddings')
        self.token_type_embeddings = tf.keras.layers.Embedding(config.type_vocab_size, config.hidden_size, name='token_type_embeddings')

        # self.LayerNorm is not snake-cased to stick with TensorFlow model variable name and be able to load
        # any TensorFlow checkpoint file
        self.LayerNorm = tf.keras.layers.LayerNormalization(epsilon=config.layer_norm_eps, name='LayerNorm')
        self.dropout = tf.keras.layers.Dropout(config.hidden_dropout_prob)

    def call(self, inputs, training=False):
        input_ids, position_ids, token_type_ids = inputs

        seq_length = tf.shape(input_ids)[1]
        if position_ids is None:
            position_ids = tf.range(seq_length, dtype=tf.int32)[tf.newaxis, :]
        if token_type_ids is None:
            token_type_ids = tf.fill(tf.shape(input_ids), 0)

        words_embeddings = self.word_embeddings(input_ids)
        position_embeddings = self.position_embeddings(position_ids)
        token_type_embeddings = self.token_type_embeddings(token_type_ids)

        embeddings = words_embeddings + position_embeddings + token_type_embeddings
        embeddings = self.LayerNorm(embeddings)
        if training:
            embeddings = self.dropout(embeddings)
        return embeddings


class TFBertSelfAttention(tf.keras.layers.Layer):
    def __init__(self, config, **kwargs):
        super(TFBertSelfAttention, self).__init__(**kwargs)
        if config.hidden_size % config.num_attention_heads != 0:
            raise ValueError(
                "The hidden size (%d) is not a multiple of the number of attention "
                "heads (%d)" % (config.hidden_size, config.num_attention_heads))
        self.output_attentions = config.output_attentions

        self.num_attention_heads = config.num_attention_heads
        assert config.hidden_size % config.num_attention_heads == 0
        self.attention_head_size = int(config.hidden_size / config.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.query = tf.keras.layers.Dense(self.all_head_size, name='query')
        self.key = tf.keras.layers.Dense(self.all_head_size, name='key')
        self.value = tf.keras.layers.Dense(self.all_head_size, name='value')

        self.dropout = tf.keras.layers.Dropout(config.attention_probs_dropout_prob)

    def transpose_for_scores(self, x, batch_size):
        x = tf.reshape(x, (batch_size, -1, self.num_attention_heads, self.attention_head_size))
        return tf.transpose(x, perm=[0, 2, 1, 3])

    def call(self, inputs, training=False):
        hidden_states, attention_mask, head_mask = inputs

        batch_size = tf.shape(hidden_states)[0]
        mixed_query_layer = self.query(hidden_states)
        mixed_key_layer = self.key(hidden_states)
        mixed_value_layer = self.value(hidden_states)

        query_layer = self.transpose_for_scores(mixed_query_layer, batch_size)
        key_layer = self.transpose_for_scores(mixed_key_layer, batch_size)
        value_layer = self.transpose_for_scores(mixed_value_layer, batch_size)

        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores = tf.matmul(query_layer, key_layer, transpose_b=True)  # (batch size, num_heads, seq_len_q, seq_len_k)
        dk = tf.cast(tf.shape(key_layer)[-1], tf.float32) # scale attention_scores
        attention_scores = attention_scores / tf.math.sqrt(dk)
        # Apply the attention mask is (precomputed for all layers in TFBertModel call() function)
        attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = tf.nn.softmax(attention_scores, axis=-1)

        if training:
            # This is actually dropping out entire tokens to attend to, which might
            # seem a bit unusual, but is taken from the original Transformer paper.
            attention_probs = self.dropout(attention_probs)

        # Mask heads if we want to
        if head_mask is not None:
            attention_probs = attention_probs * head_mask

        context_layer = tf.matmul(attention_probs, value_layer)

        context_layer = tf.transpose(context_layer, perm=[0, 2, 1, 3])
        context_layer = tf.reshape(context_layer, 
                                  (batch_size, -1, self.all_head_size))  # (batch_size, seq_len_q, all_head_size)

        outputs = (context_layer, attention_probs) if self.output_attentions else (context_layer,)
        return outputs


class TFBertSelfOutput(tf.keras.layers.Layer):
    def __init__(self, config, **kwargs):
        super(TFBertSelfOutput, self).__init__(**kwargs)
        self.dense = tf.keras.layers.Dense(config.hidden_size, name='dense')
        self.LayerNorm = tf.keras.layers.LayerNormalization(epsilon=config.layer_norm_eps, name='LayerNorm')
        self.dropout = tf.keras.layers.Dropout(config.hidden_dropout_prob)

    def call(self, inputs, training=False):
        hidden_states, input_tensor = inputs

        hidden_states = self.dense(hidden_states)
        if training:
            hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class TFBertAttention(tf.keras.layers.Layer):
    def __init__(self, config, **kwargs):
        super(TFBertAttention, self).__init__(**kwargs)
        self.self_attention = TFBertSelfAttention(config, name='self')
        self.dense_output = TFBertSelfOutput(config, name='output')

    def prune_heads(self, heads):
        raise NotImplementedError

    def call(self, inputs, training=False):
        input_tensor, attention_mask, head_mask = inputs

        self_outputs = self.self_attention([input_tensor, attention_mask, head_mask], training=training)
        attention_output = self.dense_output([self_outputs[0], input_tensor], training=training)
        outputs = (attention_output,) + self_outputs[1:]  # add attentions if we output them
        return outputs


class TFBertIntermediate(tf.keras.layers.Layer):
    def __init__(self, config, **kwargs):
        super(TFBertIntermediate, self).__init__(**kwargs)
        self.dense = tf.keras.layers.Dense(config.intermediate_size, name='dense')
        if isinstance(config.hidden_act, str) or (sys.version_info[0] == 2 and isinstance(config.hidden_act, unicode)):
            self.intermediate_act_fn = ACT2FN[config.hidden_act]
        else:
            self.intermediate_act_fn = config.hidden_act

    def call(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.intermediate_act_fn(hidden_states)
        return hidden_states


class TFBertOutput(tf.keras.layers.Layer):
    def __init__(self, config, **kwargs):
        super(TFBertOutput, self).__init__(**kwargs)
        self.dense = tf.keras.layers.Dense(config.hidden_size, name='dense')
        self.LayerNorm = tf.keras.layers.LayerNormalization(epsilon=config.layer_norm_eps, name='LayerNorm')
        self.dropout = tf.keras.layers.Dropout(config.hidden_dropout_prob)

    def call(self, inputs, training=False):
        hidden_states, input_tensor = inputs

        hidden_states = self.dense(hidden_states)
        if training:
            hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class TFBertLayer(tf.keras.layers.Layer):
    def __init__(self, config, **kwargs):
        super(TFBertLayer, self).__init__(**kwargs)
        self.attention = TFBertAttention(config, name='attention')
        self.intermediate = TFBertIntermediate(config, name='intermediate')
        self.bert_output = TFBertOutput(config, name='output')

    def call(self, inputs, training=False):
        hidden_states, attention_mask, head_mask = inputs

        attention_outputs = self.attention([hidden_states, attention_mask, head_mask], training=training)
        attention_output = attention_outputs[0]
        intermediate_output = self.intermediate(attention_output)
        layer_output = self.bert_output([intermediate_output, attention_output], training=training)
        outputs = (layer_output,) + attention_outputs[1:]  # add attentions if we output them
        return outputs


class TFBertEncoder(tf.keras.layers.Layer):
    def __init__(self, config, **kwargs):
        super(TFBertEncoder, self).__init__(**kwargs)
        self.output_attentions = config.output_attentions
        self.output_hidden_states = config.output_hidden_states
        self.layer = [TFBertLayer(config, name='layer_{}'.format(i)) for i in range(config.num_hidden_layers)]

    def call(self, inputs, training=False):
        hidden_states, attention_mask, head_mask = inputs

        all_hidden_states = ()
        all_attentions = ()
        for i, layer_module in enumerate(self.layer):
            if self.output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

            layer_outputs = layer_module([hidden_states, attention_mask, head_mask[i]], training=training)
            hidden_states = layer_outputs[0]

            if self.output_attentions:
                all_attentions = all_attentions + (layer_outputs[1],)

        # Add last layer
        if self.output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        outputs = (hidden_states,)
        if self.output_hidden_states:
            outputs = outputs + (all_hidden_states,)
        if self.output_attentions:
            outputs = outputs + (all_attentions,)
        return outputs  # outputs, (hidden states), (attentions)


class TFBertPooler(tf.keras.layers.Layer):
    def __init__(self, config, **kwargs):
        super(TFBertPooler, self).__init__(**kwargs)
        self.dense = tf.keras.layers.Dense(config.hidden_size, activation='tanh', name='dense')

    def call(self, hidden_states):
        # We "pool" the model by simply taking the hidden state corresponding
        # to the first token.
        first_token_tensor = hidden_states[:, 0]
        pooled_output = self.dense(first_token_tensor)
        return pooled_output


class TFBertPredictionHeadTransform(tf.keras.layers.Layer):
    def __init__(self, config, **kwargs):
        super(TFBertPredictionHeadTransform, self).__init__(**kwargs)
        self.dense = tf.keras.layers.Dense(config.hidden_size, name='dense')
        if isinstance(config.hidden_act, str) or (sys.version_info[0] == 2 and isinstance(config.hidden_act, unicode)):
            self.transform_act_fn = ACT2FN[config.hidden_act]
        else:
            self.transform_act_fn = config.hidden_act
        self.LayerNorm = tf.keras.layers.LayerNormalization(epsilon=config.layer_norm_eps, name='LayerNorm')

    def call(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.transform_act_fn(hidden_states)
        hidden_states = self.LayerNorm(hidden_states)
        return hidden_states


class TFBertLMPredictionHead(tf.keras.layers.Layer):
    def __init__(self, config, **kwargs):
        super(TFBertLMPredictionHead, self).__init__(**kwargs)
        self.vocab_size = config.vocab_size
        self.transform = TFBertPredictionHeadTransform(config, name='transform')

        # The output weights are the same as the input embeddings, but there is
        # an output-only bias for each token.
        self.decoder = tf.keras.layers.Dense(config.vocab_size, use_bias=False, name='decoder')

    def build(self, input_shape):
        self.bias = self.add_weight(shape=(self.vocab_size,),
                                    initializer='zeros',
                                    trainable=True,
                                    name='bias')

    def call(self, hidden_states):
        hidden_states = self.transform(hidden_states)
        hidden_states = self.decoder(hidden_states) + self.bias
        return hidden_states


class TFBertMLMHead(tf.keras.layers.Layer):
    def __init__(self, config, **kwargs):
        super(TFBertMLMHead, self).__init__(**kwargs)
        self.predictions = TFBertLMPredictionHead(config, name='predictions')

    def call(self, sequence_output):
        prediction_scores = self.predictions(sequence_output)
        return prediction_scores


class TFBertNSPHead(tf.keras.layers.Layer):
    def __init__(self, config, **kwargs):
        super(TFBertNSPHead, self).__init__(**kwargs)
        self.seq_relationship = tf.keras.layers.Dense(2, name='seq_relationship')

    def call(self, pooled_output):
        seq_relationship_score = self.seq_relationship(pooled_output)
        return seq_relationship_score


class TFBertMainLayer(tf.keras.layers.Layer):
    def __init__(self, config, **kwargs):
        super(TFBertMainLayer, self).__init__(**kwargs)
        self.num_hidden_layers = config.num_hidden_layers

        self.embeddings = TFBertEmbeddings(config, name='embeddings')
        self.encoder = TFBertEncoder(config, name='encoder')
        self.pooler = TFBertPooler(config, name='pooler')

        # self.apply(self.init_weights)  # TODO check weights initialization

    def _resize_token_embeddings(self, new_num_tokens):
        raise NotImplementedError

    def _prune_heads(self, heads_to_prune):
        """ Prunes heads of the model.
            heads_to_prune: dict of {layer_num: list of heads to prune in this layer}
            See base class PreTrainedModel
        """
        raise NotImplementedError

    def call(self, inputs, training=False):
        if not isinstance(inputs, (dict, tuple, list)):
            input_ids = inputs
            attention_mask, head_mask, position_ids, token_type_ids = None, None, None, None
        elif isinstance(inputs, (tuple, list)):
            input_ids = inputs[0]
            attention_mask = inputs[1] if len(inputs) > 1 else None
            token_type_ids = inputs[2] if len(inputs) > 2 else None
            position_ids = inputs[3] if len(inputs) > 3 else None
            head_mask = inputs[4] if len(inputs) > 4 else None
            assert len(inputs) <= 5, "Too many inputs."
        else:
            input_ids = inputs.pop('input_ids')
            attention_mask = inputs.pop('attention_mask', None)
            token_type_ids = inputs.pop('token_type_ids', None)
            position_ids = inputs.pop('position_ids', None)
            head_mask = inputs.pop('head_mask', None)
            assert len(inputs) == 0, "Unexpected inputs detected: {}. Check inputs dict key names.".format(list(inputs.keys()))

        if attention_mask is None:
            attention_mask = tf.fill(tf.shape(input_ids), 1)
        if token_type_ids is None:
            token_type_ids = tf.fill(tf.shape(input_ids), 0)

        # We create a 3D attention mask from a 2D tensor mask.
        # Sizes are [batch_size, 1, 1, to_seq_length]
        # So we can broadcast to [batch_size, num_heads, from_seq_length, to_seq_length]
        # this attention mask is more simple than the triangular masking of causal attention
        # used in OpenAI GPT, we just need to prepare the broadcast dimension here.
        extended_attention_mask = attention_mask[:, tf.newaxis, tf.newaxis, :]

        # Since attention_mask is 1.0 for positions we want to attend and 0.0 for
        # masked positions, this operation will create a tensor which is 0.0 for
        # positions we want to attend and -10000.0 for masked positions.
        # Since we are adding it to the raw scores before the softmax, this is
        # effectively the same as removing these entirely.

        extended_attention_mask = tf.cast(extended_attention_mask, tf.float32)
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0

        # Prepare head mask if needed
        # 1.0 in head_mask indicate we keep the head
        # attention_probs has shape bsz x n_heads x N x N
        # input head_mask has shape [num_heads] or [num_hidden_layers x num_heads]
        # and head_mask is converted to shape [num_hidden_layers x batch x num_heads x seq_length x seq_length]
        if not head_mask is None:
            raise NotImplementedError
        else:
            head_mask = [None] * self.num_hidden_layers
            # head_mask = tf.constant([0] * self.num_hidden_layers)

        embedding_output = self.embeddings([input_ids, position_ids, token_type_ids], training=training)
        encoder_outputs = self.encoder([embedding_output, extended_attention_mask, head_mask], training=training)

        sequence_output = encoder_outputs[0]
        pooled_output = self.pooler(sequence_output)

        outputs = (sequence_output, pooled_output,) + encoder_outputs[1:]  # add hidden_states and attentions if they are here
        return outputs  # sequence_output, pooled_output, (hidden_states), (attentions)

class TFBertPreTrainedModel(TFPreTrainedModel):
    """ An abstract class to handle weights initialization and
        a simple interface for dowloading and loading pretrained models.
    """
    config_class = BertConfig
    pretrained_model_archive_map = TF_BERT_PRETRAINED_MODEL_ARCHIVE_MAP
    load_pt_weights = load_pt_weights_in_bert
    base_model_prefix = "bert"

    def __init__(self, *inputs, **kwargs):
        super(TFBertPreTrainedModel, self).__init__(*inputs, **kwargs)

    def init_weights(self, module):
        """ Initialize the weights.
        """
        raise NotImplementedError


BERT_START_DOCSTRING = r"""    The BERT model was proposed in
    `BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding`_
    by Jacob Devlin, Ming-Wei Chang, Kenton Lee and Kristina Toutanova. It's a bidirectional transformer
    pre-trained using a combination of masked language modeling objective and next sentence prediction
    on a large corpus comprising the Toronto Book Corpus and Wikipedia.

    This model is a tf.keras.Model `tf.keras.Model`_ sub-class. Use it as a regular TF 2.0 Keras Model and
    refer to the TF 2.0 documentation for all matter related to general usage and behavior.

    .. _`BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding`:
        https://arxiv.org/abs/1810.04805

    .. _`tf.keras.Model`:
        https://www.tensorflow.org/versions/r2.0/api_docs/python/tf/keras/Model

    Important note on the model inputs:
        The inputs of the TF 2.0 models are slightly different from the PyTorch ones since
        TF 2.0 Keras doesn't accept named arguments with defaults values for input Tensor.
        More precisely, input Tensors are gathered in the first arguments of the model call function: `model(inputs)`.
        There are three possibilities to gather and feed the inputs to the model:

        - a single Tensor with input_ids only and nothing else: `model(inputs_ids)
        - a list of varying length with one or several input Tensors IN THE ORDER given in the docstring:
            `model([input_ids, attention_mask])` or `model([input_ids, attention_mask, token_type_ids])`
        - a dictionary with one or several input Tensors associaed to the input names given in the docstring:
            `model({'input_ids': input_ids, 'token_type_ids': token_type_ids})`

    Parameters:
        config (:class:`~pytorch_transformers.BertConfig`): Model configuration class with all the parameters of the model. 
            Initializing with a config file does not load the weights associated with the model, only the configuration.
            Check out the :meth:`~pytorch_transformers.PreTrainedModel.from_pretrained` method to load the model weights.
"""

BERT_INPUTS_DOCSTRING = r"""
    Inputs:
        **input_ids**: ``torch.LongTensor`` of shape ``(batch_size, sequence_length)``:
            Indices of input sequence tokens in the vocabulary.
            To match pre-training, BERT input sequence should be formatted with [CLS] and [SEP] tokens as follows:

            (a) For sequence pairs:

                ``tokens:         [CLS] is this jack ##son ##ville ? [SEP] no it is not . [SEP]``
                
                ``token_type_ids:   0   0  0    0    0     0       0   0   1  1  1  1   1   1``

            (b) For single sequences:

                ``tokens:         [CLS] the dog is hairy . [SEP]``
                
                ``token_type_ids:   0   0   0   0  0     0   0``

            Bert is a model with absolute position embeddings so it's usually advised to pad the inputs on
            the right rather than the left.

            Indices can be obtained using :class:`pytorch_transformers.BertTokenizer`.
            See :func:`pytorch_transformers.PreTrainedTokenizer.encode` and
            :func:`pytorch_transformers.PreTrainedTokenizer.convert_tokens_to_ids` for details.
        **attention_mask**: (`optional`) ``torch.FloatTensor`` of shape ``(batch_size, sequence_length)``:
            Mask to avoid performing attention on padding token indices.
            Mask values selected in ``[0, 1]``:
            ``1`` for tokens that are NOT MASKED, ``0`` for MASKED tokens.
        **token_type_ids**: (`optional`) ``torch.LongTensor`` of shape ``(batch_size, sequence_length)``:
            Segment token indices to indicate first and second portions of the inputs.
            Indices are selected in ``[0, 1]``: ``0`` corresponds to a `sentence A` token, ``1``
            corresponds to a `sentence B` token
            (see `BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding`_ for more details).
        **position_ids**: (`optional`) ``torch.LongTensor`` of shape ``(batch_size, sequence_length)``:
            Indices of positions of each input sequence tokens in the position embeddings.
            Selected in the range ``[0, config.max_position_embeddings - 1]``.
        **head_mask**: (`optional`) ``torch.FloatTensor`` of shape ``(num_heads,)`` or ``(num_layers, num_heads)``:
            Mask to nullify selected heads of the self-attention modules.
            Mask values selected in ``[0, 1]``:
            ``1`` indicates the head is **not masked**, ``0`` indicates the head is **masked**.
"""

@add_start_docstrings("The bare Bert Model transformer outputing raw hidden-states without any specific head on top.",
                      BERT_START_DOCSTRING, BERT_INPUTS_DOCSTRING)
class TFBertModel(TFBertPreTrainedModel):
    r"""
    Outputs: `Tuple` comprising various elements depending on the configuration (config) and inputs:
        **last_hidden_state**: ``torch.FloatTensor`` of shape ``(batch_size, sequence_length, hidden_size)``
            Sequence of hidden-states at the output of the last layer of the model.
        **pooler_output**: ``torch.FloatTensor`` of shape ``(batch_size, hidden_size)``
            Last layer hidden-state of the first token of the sequence (classification token)
            further processed by a Linear layer and a Tanh activation function. The Linear
            layer weights are trained from the next sentence prediction (classification)
            objective during Bert pretraining. This output is usually *not* a good summary
            of the semantic content of the input, you're often better with averaging or pooling
            the sequence of hidden-states for the whole input sequence.
        **hidden_states**: (`optional`, returned when ``config.output_hidden_states=True``)
            list of ``torch.FloatTensor`` (one for the output of each layer + the output of the embeddings)
            of shape ``(batch_size, sequence_length, hidden_size)``:
            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        **attentions**: (`optional`, returned when ``config.output_attentions=True``)
            list of ``torch.FloatTensor`` (one for each layer) of shape ``(batch_size, num_heads, sequence_length, sequence_length)``:
            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention heads.

    Examples::

        tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
        model = TFBertModel.from_pretrained('bert-base-uncased')
        input_ids = tf.tensor(tokenizer.encode("Hello, my dog is cute")).unsqueeze(0)  # Batch size 1
        outputs = model(input_ids)
        last_hidden_states = outputs[0]  # The last hidden-state is the first element of the output tuple

    """
    def __init__(self, config):
        super(TFBertModel, self).__init__(config)
        self.bert = TFBertMainLayer(config, name='bert')

    def call(self, inputs, training=False):
        outputs = self.bert(inputs, training=training)
        return outputs


@add_start_docstrings("""Bert Model with two heads on top as done during the pre-training:
    a `masked language modeling` head and a `next sentence prediction (classification)` head. """,
    BERT_START_DOCSTRING, BERT_INPUTS_DOCSTRING)
class TFBertForPreTraining(TFBertPreTrainedModel):
    r"""
        **masked_lm_labels**: (`optional`) ``torch.LongTensor`` of shape ``(batch_size, sequence_length)``:
            Labels for computing the masked language modeling loss.
            Indices should be in ``[-1, 0, ..., config.vocab_size]`` (see ``input_ids`` docstring)
            Tokens with indices set to ``-1`` are ignored (masked), the loss is only computed for the tokens with labels
            in ``[0, ..., config.vocab_size]``
        **next_sentence_label**: (`optional`) ``torch.LongTensor`` of shape ``(batch_size,)``:
            Labels for computing the next sequence prediction (classification) loss. Input should be a sequence pair (see ``input_ids`` docstring)
            Indices should be in ``[0, 1]``.
            ``0`` indicates sequence B is a continuation of sequence A,
            ``1`` indicates sequence B is a random sequence.

    Outputs: `Tuple` comprising various elements depending on the configuration (config) and inputs:
        **loss**: (`optional`, returned when both ``masked_lm_labels`` and ``next_sentence_label`` are provided) ``torch.FloatTensor`` of shape ``(1,)``:
            Total loss as the sum of the masked language modeling loss and the next sequence prediction (classification) loss.
        **prediction_scores**: ``torch.FloatTensor`` of shape ``(batch_size, sequence_length, config.vocab_size)``
            Prediction scores of the language modeling head (scores for each vocabulary token before SoftMax).
        **seq_relationship_scores**: ``torch.FloatTensor`` of shape ``(batch_size, sequence_length, 2)``
            Prediction scores of the next sequence prediction (classification) head (scores of True/False continuation before SoftMax).
        **hidden_states**: (`optional`, returned when ``config.output_hidden_states=True``)
            list of ``torch.FloatTensor`` (one for the output of each layer + the output of the embeddings)
            of shape ``(batch_size, sequence_length, hidden_size)``:
            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        **attentions**: (`optional`, returned when ``config.output_attentions=True``)
            list of ``torch.FloatTensor`` (one for each layer) of shape ``(batch_size, num_heads, sequence_length, sequence_length)``:
            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention heads.

    Examples::

        tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
        model = TFBertForPreTraining.from_pretrained('bert-base-uncased')
        input_ids = torch.tensor(tokenizer.encode("Hello, my dog is cute")).unsqueeze(0)  # Batch size 1
        outputs = model(input_ids)
        prediction_scores, seq_relationship_scores = outputs[:2]

    """
    def __init__(self, config):
        super(TFBertForPreTraining, self).__init__(config)

        self.bert = TFBertMainLayer(config, name='bert')
        self.cls_mlm = TFBertMLMHead(config, name='cls_mlm')
        self.cls_nsp = TFBertNSPHead(config, name='cls_nsp')

        # self.apply(self.init_weights)  # TODO check added weights initialization
        self.tie_weights()

    def tie_weights(self):
        """ Make sure we are sharing the input and output embeddings.
        """
        pass  # TODO add weights tying

    def call(self, inputs, training=False):
        outputs = self.bert(inputs, training=training)

        sequence_output, pooled_output = outputs[:2]
        prediction_scores = self.cls_mlm(sequence_output)
        seq_relationship_score = self.cls_nsp(pooled_output)

        outputs = (prediction_scores, seq_relationship_score,) + outputs[2:]  # add hidden states and attention if they are here

        # if masked_lm_labels is not None and next_sentence_label is not None:
        #     loss_fct = CrossEntropyLoss(ignore_index=-1)
        #     masked_lm_loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), masked_lm_labels.view(-1))
        #     next_sentence_loss = loss_fct(seq_relationship_score.view(-1, 2), next_sentence_label.view(-1))
        #     total_loss = masked_lm_loss + next_sentence_loss
        #     outputs = (total_loss,) + outputs
        # TODO add example with losses using model.compile and a dictionary of losses (give names to the output layers)

        return outputs  # prediction_scores, seq_relationship_score, (hidden_states), (attentions)


@add_start_docstrings("""Bert Model with a `language modeling` head on top. """,
    BERT_START_DOCSTRING, BERT_INPUTS_DOCSTRING)
class TFBertForMaskedLM(TFBertPreTrainedModel):
    r"""
        **masked_lm_labels**: (`optional`) ``torch.LongTensor`` of shape ``(batch_size, sequence_length)``:
            Labels for computing the masked language modeling loss.
            Indices should be in ``[-1, 0, ..., config.vocab_size]`` (see ``input_ids`` docstring)
            Tokens with indices set to ``-1`` are ignored (masked), the loss is only computed for the tokens with labels
            in ``[0, ..., config.vocab_size]``

    Outputs: `Tuple` comprising various elements depending on the configuration (config) and inputs:
        **loss**: (`optional`, returned when ``masked_lm_labels`` is provided) ``torch.FloatTensor`` of shape ``(1,)``:
            Masked language modeling loss.
        **prediction_scores**: ``torch.FloatTensor`` of shape ``(batch_size, sequence_length, config.vocab_size)``
            Prediction scores of the language modeling head (scores for each vocabulary token before SoftMax).
        **hidden_states**: (`optional`, returned when ``config.output_hidden_states=True``)
            list of ``torch.FloatTensor`` (one for the output of each layer + the output of the embeddings)
            of shape ``(batch_size, sequence_length, hidden_size)``:
            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        **attentions**: (`optional`, returned when ``config.output_attentions=True``)
            list of ``torch.FloatTensor`` (one for each layer) of shape ``(batch_size, num_heads, sequence_length, sequence_length)``:
            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention heads.

    Examples::

        tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
        model = TFBertForMaskedLM.from_pretrained('bert-base-uncased')
        input_ids = torch.tensor(tokenizer.encode("Hello, my dog is cute")).unsqueeze(0)  # Batch size 1
        outputs = model(input_ids, masked_lm_labels=input_ids)
        loss, prediction_scores = outputs[:2]

    """
    def __init__(self, config):
        super(TFBertForMaskedLM, self).__init__(config)

        self.bert = TFBertMainLayer(config, name='bert')
        self.cls_mlm = TFBertMLMHead(config, name='cls_mlm')

        # self.apply(self.init_weights)
        self.tie_weights()

    def tie_weights(self):
        """ Make sure we are sharing the input and output embeddings.
        """
        pass  # TODO add weights tying

    def call(self, inputs, training=False):
        outputs = self.bert(inputs, training=training)

        sequence_output = outputs[0]
        prediction_scores = self.cls_mlm(sequence_output)

        outputs = (prediction_scores,) + outputs[2:]  # Add hidden states and attention if they are here
        # if masked_lm_labels is not None:
        #     loss_fct = CrossEntropyLoss(ignore_index=-1)
        #     masked_lm_loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), masked_lm_labels.view(-1))
        #     outputs = (masked_lm_loss,) + outputs
        # TODO example with losses

        return outputs  # prediction_scores, (hidden_states), (attentions)


@add_start_docstrings("""Bert Model with a `next sentence prediction (classification)` head on top. """,
    BERT_START_DOCSTRING, BERT_INPUTS_DOCSTRING)
class TFBertForNextSentencePrediction(TFBertPreTrainedModel):
    r"""
        **next_sentence_label**: (`optional`) ``torch.LongTensor`` of shape ``(batch_size,)``:
            Labels for computing the next sequence prediction (classification) loss. Input should be a sequence pair (see ``input_ids`` docstring)
            Indices should be in ``[0, 1]``.
            ``0`` indicates sequence B is a continuation of sequence A,
            ``1`` indicates sequence B is a random sequence.

    Outputs: `Tuple` comprising various elements depending on the configuration (config) and inputs:
        **loss**: (`optional`, returned when ``next_sentence_label`` is provided) ``torch.FloatTensor`` of shape ``(1,)``:
            Next sequence prediction (classification) loss.
        **seq_relationship_scores**: ``torch.FloatTensor`` of shape ``(batch_size, sequence_length, 2)``
            Prediction scores of the next sequence prediction (classification) head (scores of True/False continuation before SoftMax).
        **hidden_states**: (`optional`, returned when ``config.output_hidden_states=True``)
            list of ``torch.FloatTensor`` (one for the output of each layer + the output of the embeddings)
            of shape ``(batch_size, sequence_length, hidden_size)``:
            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        **attentions**: (`optional`, returned when ``config.output_attentions=True``)
            list of ``torch.FloatTensor`` (one for each layer) of shape ``(batch_size, num_heads, sequence_length, sequence_length)``:
            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention heads.

    Examples::

        tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
        model = TFBertForNextSentencePrediction.from_pretrained('bert-base-uncased')
        input_ids = torch.tensor(tokenizer.encode("Hello, my dog is cute")).unsqueeze(0)  # Batch size 1
        outputs = model(input_ids)
        seq_relationship_scores = outputs[0]

    """
    def __init__(self, config):
        super(TFBertForNextSentencePrediction, self).__init__(config)

        self.bert = TFBertMainLayer(config, name='bert')
        self.cls_nsp = TFBertNSPHead(config, name='cls_nsp')

        # self.apply(self.init_weights)

    def call(self, inputs, training=False):
        outputs = self.bert(inputs, training=training)

        pooled_output = outputs[1]
        seq_relationship_score = self.cls_nsp(pooled_output)

        outputs = (seq_relationship_score,) + outputs[2:]  # add hidden states and attention if they are here
        # if next_sentence_label is not None:
        #     loss_fct = CrossEntropyLoss(ignore_index=-1)
        #     next_sentence_loss = loss_fct(seq_relationship_score.view(-1, 2), next_sentence_label.view(-1))
        #     outputs = (next_sentence_loss,) + outputs

        return outputs  # seq_relationship_score, (hidden_states), (attentions)