import math
import numpy as np
import keras_core as K
from keras_core import layers
from keras_core import initializers
from keras_core import activations

from config import GPTConfig


class CausalSelfAttention(K.layers.Layer):
    def __init__(self, config, **kwargs):
        super().__init__(**kwargs)
        assert config.hidden_size % config.n_head == 0
        # key, query, value projections for all heads, but in a batch
        self.attn = layers.Dense(
            units=config.hidden_size * 3, use_bias=config.bias,
            kernel_initializer=initializers.RandomNormal(mean=0.0, stddev=0.02),
            bias_initializer=initializers.Zeros(),
        )
        # regularization
        self.attn_drop = layers.Dropout(config.dropout)
        self.resid_drop = layers.Dropout(config.dropout)
        # output projection (special scaled init to the residual projections, per GPT-2 paper)
        self.proj = K.layers.Dense(
            units=config.hidden_size, use_bias=config.bias,
            kernel_initializer=initializers.RandomNormal(mean=0.0, stddev=0.02 / math.sqrt(2 * config.n_layer)),
            bias_initializer=initializers.Zeros(),
        )

        self.mask = K.ops.tril(K.ops.ones(shape=(1, config.block_size, config.block_size)))

        self.config = config

    def call(self, inputs, training=None):
        B, T, C = inputs.shape # batch_size, block_size, hidden_size

        # calculate query, key, values for all heads in batch and move head forward to be the batch dim
        attended = self.attn(inputs)
        splitted = K.ops.split(attended, 3, axis=2)
        q, k, v = splitted[0], splitted[1], splitted[2]
        q = K.ops.reshape(q, (B, T, self.config.n_head, C // self.config.n_head))
        q = K.ops.transpose(q, (0, 2, 1, 3))
        k = K.ops.reshape(k, (B, T, self.config.n_head, C // self.config.n_head))
        k = K.ops.transpose(k, (0, 2, 1, 3))
        v = K.ops.reshape(v, (B, T, self.config.n_head, C // self.config.n_head))
        v = K.ops.transpose(v, (0, 2, 1, 3))

        # causal self-attention; Self-attend: (B, nh, T, hs) x (B, nh, hs, T) -> (B, nh, T, T)
        att = K.ops.matmul(q, K.ops.transpose(k, (0, 1, 3, 2))) * (1.0 / math.sqrt(k.shape[-1])) # (B, nh, T, T)
        att = K.ops.where(K.ops.equal(att, 0), -np.inf, att)
        att = activations.softmax(att, axis=-1)
        att = self.attn_drop(att, training=training)
        y = K.ops.matmul(att, v) # (B, nh, T, T) x (B, nh, T, hs) -> (B, nh, T, hs)
        y = K.ops.transpose(y, (0, 2, 1, 3)) # (B, nh, T, hs) -> (B, T, nh, hs)
        y = K.ops.reshape(y, (B, T, C)) # re-assemble all head outputs side by side

        # output projection
        y = self.resid_drop(self.proj(y), training=training)
        return y


class Block(layers.Layer):
    def __init__(self, config, **kwargs):
        super().__init__(**kwargs)
        self.ln1 = layers.LayerNormalization(epsilon=config.layer_norm_epsilon)
        self.ln2 = layers.LayerNormalization(epsilon=config.layer_norm_epsilon)
        self.attn = CausalSelfAttention(config)
        self.mlp = K.Sequential([
            layers.Dense(
                units=4*config.hidden_size, use_bias=config.bias, activation="gelu",
                kernel_initializer=initializers.RandomNormal(mean=0.0, stddev=0.02),
                bias_initializer=initializers.Zeros(),
            ),
            layers.Dense(
                units=config.hidden_size, use_bias=config.bias,
                kernel_initializer=initializers.RandomNormal(mean=0.0, stddev=0.02),
                bias_initializer=initializers.Zeros(),
            ),
            layers.Dropout(config.dropout)
        ])

    def call(self, x, training=None):
        x = x + self.attn(self.ln1(x), training=training)
        x = x + self.mlp(self.ln2(x), training=training)
        return x
    

class GPT(K.Model):
    def __init__(self, config: GPTConfig, **kwargs):
        super().__init__(name="coreGPT", **kwargs)
        self.config = config

        # input embedding
        self.tok_emb = K.layers.Embedding(
            input_dim=config.vocab_size, output_dim=config.hidden_size,
            embeddings_initializer=K.initializers.RandomNormal(mean=0.0, stddev=0.02),
            name="embedding",
        )
        self.drop = layers.Dropout(config.dropout)
        # transformer blocks
        self.blocks = [Block(config) for _ in range(config.n_layer)]
        # decoder head
        self.ln_f = layers.LayerNormalization(epsilon=config.layer_norm_epsilon, axis=-1) # TODO bias
        self.head = layers.Dense(
            units=config.vocab_size, use_bias=False,
            kernel_initializer=initializers.RandomNormal(mean=0.0, stddev=0.02),
        )

    def build(self, input_shape):
        super().build(input_shape)
        self.pos_emb = self.add_weight(
            name="positional",
            shape=(1, self.config.block_size, self.config.hidden_size),
            initializer=K.initializers.RandomNormal(mean=0.0, stddev=0.02),
            trainable=True,
        )

    def call(self, inputs, training=None):
        B, T = inputs.shape
        # embed sentence
        wte = self.tok_emb(inputs)
        wpe = self.pos_emb[:, :T, :]
        x = self.drop(wte + wpe, training=training)
        # attention
        for block in self.blocks:
            x = block(x, training=training)
        # compute logits
        x = self.ln_f(x)
        x = self.head(x)
        return x
    
    def summary(self):
        x = K.Input(shape=[self.config.block_size], batch_size=self.config.batch_size, dtype="int32")
        dummy = K.Model(inputs=x, outputs=self.call(x), name=self.name)
        return dummy.summary()
