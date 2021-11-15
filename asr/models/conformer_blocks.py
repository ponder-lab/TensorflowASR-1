
import tensorflow as tf

from asr.models.wav_model import WavePickModel
from utils.tools import merge_two_last_dims
from asr.models.layers.switchnorm import SwitchNormalization
from asr.models.layers.multihead_attention import MultiHeadAttention
from asr.models.layers.time_frequency import Spectrogram,Melspectrogram
from asr.models.layers.positional_encoding import PositionalEncoding
from leaf_audio import frontend
import warnings


class WeightNormalization(tf.keras.layers.Wrapper):
    """Layer wrapper to decouple magnitude and direction of the layer's weights.
    This wrapper reparameterizes a layer by decoupling the weight's
    magnitude and direction. This speeds up convergence by improving the
    conditioning of the optimization problem. It has an optional data-dependent
    initialization scheme, in which initial values of weights are set as functions
    of the first minibatch of data. Both the weight normalization and data-
    dependent initialization are described in [Salimans and Kingma (2016)][1].
    #### Example
    ```python
      net = WeightNorm(tf.keras.layers.Conv2D(2, 2, activation='relu'),
             input_shape=(32, 32, 3), data_init=True)(x)
      net = WeightNorm(tf.keras.layers.Conv2DTranspose(16, 5, activation='relu'),
                       data_init=True)
      net = WeightNorm(tf.keras.layers.Dense(120, activation='relu'),
                       data_init=True)(net)
      net = WeightNorm(tf.keras.layers.Dense(num_classes),
                       data_init=True)(net)
    ```
    #### References
    [1]: Tim Salimans and Diederik P. Kingma. Weight Normalization: A Simple
         Reparameterization to Accelerate Training of Deep Neural Networks. In
         _30th Conference on Neural Information Processing Systems_, 2016.
         https://arxiv.org/abs/1602.07868
    """

    def __init__(self, layer, data_init=True, **kwargs):
        """Initialize WeightNorm wrapper.
        Args:
          layer: A `tf.keras.layers.Layer` instance. Supported layer types are
            `Dense`, `Conv2D`, and `Conv2DTranspose`. Layers with multiple inputs
            are not supported.
          data_init: `bool`, if `True` use data dependent variable initialization.
          **kwargs: Additional keyword args passed to `tf.keras.layers.Wrapper`.
        Raises:
          ValueError: If `layer` is not a `tf.keras.layers.Layer` instance.
        """
        if not isinstance(layer, tf.keras.layers.Layer):
            raise ValueError(
                'Please initialize `WeightNorm` layer with a `tf.keras.layers.Layer` '
                'instance. You passed: {input}'.format(input=layer))

        layer_type = type(layer).__name__
        if layer_type not in ['Dense', 'Conv2D', 'Conv2DTranspose', "Conv1D", "GroupConv1D"]:
            warnings.warn('`WeightNorm` is tested only for `Dense`, `Conv2D`, `Conv1D`, `GroupConv1D`, '
                          '`GroupConv2D`, and `Conv2DTranspose` layers. You passed a layer of type `{}`'
                          .format(layer_type))

        super().__init__(layer, **kwargs)

        self.data_init = data_init
        self._track_trackable(layer, name='layer')
        self.filter_axis = -2 if layer_type == 'Conv2DTranspose' else -1

    def _compute_weights(self):
        """Generate weights with normalization."""
        # Determine the axis along which to expand `g` so that `g` broadcasts to
        # the shape of `v`.
        new_axis = -self.filter_axis - 3

        self.layer.kernel = tf.nn.l2_normalize(
            self.v, axis=self.kernel_norm_axes) * tf.expand_dims(self.g, new_axis)

    def _init_norm(self):
        """Set the norm of the weight vector."""
        kernel_norm = tf.sqrt(
            tf.reduce_sum(tf.square(self.v), axis=self.kernel_norm_axes))
        self.g.assign(kernel_norm)

    def _data_dep_init(self, inputs):
        """Data dependent initialization."""
        # Normalize kernel first so that calling the layer calculates
        # `tf.dot(v, x)/tf.norm(v)` as in (5) in ([Salimans and Kingma, 2016][1]).
        self._compute_weights()

        activation = self.layer.activation
        self.layer.activation = None

        use_bias = self.layer.bias is not None
        if use_bias:
            bias = self.layer.bias
            self.layer.bias = tf.zeros_like(bias)

        # Since the bias is initialized as zero, setting the activation to zero and
        # calling the initialized layer (with normalized kernel) yields the correct
        # computation ((5) in Salimans and Kingma (2016))
        x_init = self.layer(inputs)
        norm_axes_out = list(range(x_init.shape.rank - 1))
        m_init, v_init = tf.nn.moments(x_init, norm_axes_out)
        scale_init = 1. / tf.sqrt(v_init + 1e-10)

        self.g.assign(self.g * scale_init)
        if use_bias:
            self.layer.bias = bias
            self.layer.bias.assign(-m_init * scale_init)
        self.layer.activation = activation

    def build(self, input_shape=None):
        """Build `Layer`.
        Args:
          input_shape: The shape of the input to `self.layer`.
        Raises:
          ValueError: If `Layer` does not contain a `kernel` of weights
        """
        if not self.layer.built:
            self.layer.build(input_shape)

            if not hasattr(self.layer, 'kernel'):
                raise ValueError('`WeightNorm` must wrap a layer that'
                                 ' contains a `kernel` for weights')

            self.kernel_norm_axes = list(range(self.layer.kernel.shape.ndims))
            self.kernel_norm_axes.pop(self.filter_axis)

            self.v = self.layer.kernel

            # to avoid a duplicate `kernel` variable after `build` is called
            self.layer.kernel = None
            self.g = self.add_weight(
                name='g',
                shape=(int(self.v.shape[self.filter_axis]),),
                initializer='ones',
                dtype=self.v.dtype,
                trainable=True)
            self.initialized = self.add_weight(
                name='initialized',
                dtype=tf.bool,
                trainable=False)
            self.initialized.assign(False)

        super().build()

    def call(self, inputs):
        """Call `Layer`."""
        if not self.initialized:
            if self.data_init:
                self._data_dep_init(inputs)
            else:
                # initialize `g` as the norm of the initialized kernel
                self._init_norm()

            self.initialized.assign(True)

        self._compute_weights()
        output = self.layer(inputs)
        return output

    def compute_output_shape(self, input_shape):
        return tf.TensorShape(
            self.layer.compute_output_shape(input_shape).as_list())



class GLU(tf.keras.layers.Layer):
    def __init__(self,
                 axis=-1,
                 name="glu_activation",
                 **kwargs):
        super(GLU, self).__init__(name=name, **kwargs)
        self.axis = axis

    def call(self, inputs, **kwargs):
        a, b = tf.split(inputs, 2, axis=self.axis)
        b = tf.nn.sigmoid(b)
        return tf.multiply(a, b)

    def get_config(self):
        conf = super(GLU, self).get_config()
        conf.update({"axis": self.axis})
        return conf
class SEModule(tf.keras.layers.Layer):
    def __init__(
        self,
        kernel_size: int = 3,
        filters: int = 144,
        dropout: float=0.1,
        **kwargs,
    ):
        super(SEModule, self).__init__(**kwargs)
        assert filters%8==0,'dmodel must be Nx8'
        self.conv = ConvModule(
            input_dim=filters,
            kernel_size=kernel_size,
            dropout=dropout,
            name=f"{self.name}_conv_module",
        )
        self.activation = tf.keras.layers.Activation(
            tf.keras.activations.swish, name="swish_activation")
        self.fc1 = WeightNormalization(tf.keras.layers.Dense(filters // 8, name=f"{self.name}_fc1"))
        self.fc2 = WeightNormalization(tf.keras.layers.Dense(filters, name=f"{self.name}_fc2"))

    def call(
        self,
        inputs,
        training=False,
        **kwargs,
    ):
        input_length=tf.stack([tf.shape(inputs)[1]])

        outputs = self.conv(inputs, training=training)

        se = tf.divide(tf.reduce_sum(outputs, axis=1), tf.expand_dims(tf.cast(input_length, dtype=outputs.dtype), axis=1))
        se = self.fc1(se, training=training)
        se = self.activation(se)
        se = self.fc2(se, training=training)
        se = self.activation(se)
        se = tf.nn.sigmoid(se)
        se = tf.expand_dims(se, axis=1)

        outputs = tf.multiply(outputs, se)
        return outputs
class ConvSubsampling(tf.keras.layers.Layer):
    def __init__(self,
                 odim: int,
                 reduction_factor: int = 4,
                 dropout: float = 0.0,
                 name="conv_subsampling",
                 **kwargs):
        super(ConvSubsampling, self).__init__(name=name, **kwargs)
        assert reduction_factor % 2 == 0, "reduction_factor must be divisible by 2"
        self.conv1 =WeightNormalization( tf.keras.layers.Conv2D(
            filters=odim, kernel_size=(3, 3),
            strides=((reduction_factor // 2), 2),
            padding="same", activation="relu"
        ))
        self.conv2 = WeightNormalization(tf.keras.layers.Conv2D(
            filters=odim, kernel_size=(3, 3),
            strides=(2, 2), padding="same",
            activation="relu"
        ))
        self.linear = WeightNormalization(tf.keras.layers.Dense(odim))
        self.do = tf.keras.layers.Dropout(dropout)

    # @tf.function(experimental_relax_shapes=True)
    def call(self, inputs, training=False, **kwargs):
        outputs = self.conv1(inputs, training=training)
        outputs = self.conv2(outputs, training=training)

        outputs = merge_two_last_dims(outputs)
        outputs = self.linear(outputs, training=training)
        return self.do(outputs, training=training)

    def get_config(self):
        conf = super(ConvSubsampling, self).get_config()
        conf.update(self.conv1.get_config())
        conf.update(self.conv2.get_config())
        conf.update(self.linear.get_config())
        conf.update(self.do.get_config())
        return conf


class FFModule(tf.keras.layers.Layer):
    def __init__(self,
                 input_dim,
                 dropout=0.0,
                 fc_factor=0.5,
                 name="ff_module",
                 **kwargs):
        super(FFModule, self).__init__(name=name, **kwargs)
        self.fc_factor = fc_factor
        self.ln = tf.keras.layers.LayerNormalization()
        self.ffn1 = WeightNormalization(tf.keras.layers.Dense(4 * input_dim))
        self.swish = tf.keras.layers.Activation(
            tf.keras.activations.swish, name="swish_activation")
        self.do1 = tf.keras.layers.Dropout(dropout)
        self.ffn2 = WeightNormalization(tf.keras.layers.Dense(input_dim))
        self.do2 = tf.keras.layers.Dropout(dropout)
        self.res_add = tf.keras.layers.Add()

    # @tf.function(experimental_relax_shapes=True)
    def call(self, inputs, training=False, **kwargs):
        outputs = self.ln(inputs, training=training)
        outputs = self.ffn1(outputs, training=training)
        outputs = self.swish(outputs)
        outputs = self.do1(outputs, training=training)
        outputs = self.ffn2(outputs, training=training)
        outputs = self.do2(outputs, training=training)
        outputs = self.res_add([inputs, self.fc_factor * outputs])
        return outputs

    def get_config(self):
        conf = super(FFModule, self).get_config()
        conf.update({"fc_factor": self.fc_factor})
        conf.update(self.ln.get_config())
        conf.update(self.ffn1.get_config())
        conf.update(self.swish.get_config())
        conf.update(self.do1.get_config())
        conf.update(self.ffn2.get_config())
        conf.update(self.do2.get_config())
        conf.update(self.res_add.get_config())
        return conf


class MHSAModule(tf.keras.layers.Layer):
    def __init__(self,
                 head_size,
                 num_heads,
                 dropout=0.0,
                 name="mhsa_module",
                 **kwargs):
        super(MHSAModule, self).__init__(name=name, **kwargs)
        # self.pc = PositionalEncoding()
        self.ln = tf.keras.layers.LayerNormalization()
        self.mha =MultiHeadAttention(head_size=head_size, num_heads=num_heads)
        self.do = tf.keras.layers.Dropout(dropout)
        self.res_add = tf.keras.layers.Add()

    # @tf.function(experimental_relax_shapes=True)
    def call(self, inputs, training=False, **kwargs):
        # outputs = self.pc(inputs)
        outputs = self.ln(inputs, training=training)
        outputs = self.mha([outputs, outputs, outputs], training=training)
        outputs = self.do(outputs, training=training)
        outputs = self.res_add([inputs, outputs])
        return outputs

    def get_config(self):
        conf = super(MHSAModule, self).get_config()
        # conf.update(self.pc.get_config())
        conf.update(self.ln.get_config())
        conf.update(self.mha.get_config())
        conf.update(self.do.get_config())
        conf.update(self.res_add.get_config())
        return conf


class ConvModule(tf.keras.layers.Layer):
    def __init__(self,
                 input_dim,
                 kernel_size=32,
                 dropout=0.0,
                 name="conv_module",
                 **kwargs):
        super(ConvModule, self).__init__(name=name, **kwargs)
        self.ln = tf.keras.layers.LayerNormalization()
        self.pw_conv_1 = WeightNormalization(tf.keras.layers.Conv1D(
            filters=2 * input_dim, kernel_size=1, strides=1,
            padding="same", name="pw_conv_1"
        ))
        self.glu = GLU()
        self.dw_conv = tf.keras.layers.SeparableConv1D(
            filters=2 * input_dim, kernel_size=kernel_size, strides=1,
            padding="same", depth_multiplier=1, name="dw_conv"
        )
        self.bn =SwitchNormalization()
        self.swish = tf.keras.layers.Activation(
            tf.keras.activations.swish, name="swish_activation")
        self.pw_conv_2 = WeightNormalization(tf.keras.layers.Conv1D(filters=input_dim, kernel_size=1, strides=1,
                                                padding="same", name="pw_conv_2"))
        self.do = tf.keras.layers.Dropout(dropout)
        self.res_add = tf.keras.layers.Add()

    # @tf.function(experimental_relax_shapes=True)
    def call(self, inputs, training=False, **kwargs):
        outputs = self.ln(inputs, training=training)
        outputs = self.pw_conv_1(outputs, training=training)
        outputs = self.glu(outputs)
        outputs = self.dw_conv(outputs, training=training)
        outputs = self.bn(outputs, training=training)
        outputs = self.swish(outputs)
        outputs = self.pw_conv_2(outputs, training=training)
        outputs = self.do(outputs, training=training)
        outputs = self.res_add([inputs, outputs])
        return outputs

    def get_config(self):
        conf = super(ConvModule, self).get_config()
        conf.update(self.ln.get_config())
        conf.update(self.pw_conv_1.get_config())
        conf.update(self.glu.get_config())
        conf.update(self.dw_conv.get_config())
        conf.update(self.bn.get_config())
        conf.update(self.swish.get_config())
        conf.update(self.pw_conv_2.get_config())
        conf.update(self.do.get_config())
        conf.update(self.res_add.get_config())
        return conf


class ConformerBlock(tf.keras.layers.Layer):
    def __init__(self,
                 input_dim,
                 dropout=0.0,
                 fc_factor=0.5,
                 head_size=144,
                 num_heads=4,
                 kernel_size=32,
                 name="conformer_block",
                 **kwargs):
        super(ConformerBlock, self).__init__(name=name, **kwargs)
        self.ffm1 = FFModule(input_dim=input_dim,
                             dropout=dropout, fc_factor=fc_factor,
                             name="ff_module_1")
        self.mhsam = MHSAModule(head_size=head_size, num_heads=num_heads,
                                dropout=dropout)
        self.convm = SEModule(filters=input_dim, kernel_size=kernel_size,
                                dropout=dropout)
        self.ffm2 = FFModule(input_dim=input_dim,
                             dropout=dropout, fc_factor=fc_factor,
                             name="ff_module_2")
        self.ln = tf.keras.layers.LayerNormalization()

    # @tf.function(experimental_relax_shapes=True)
    def call(self, inputs, training=False, **kwargs):
        outputs = self.ffm1(inputs, training=training)
        outputs = self.mhsam(outputs, training=training)
        outputs = self.convm(outputs, training=training)
        outputs = self.ffm2(outputs, training=training)
        outputs = self.ln(outputs, training=training)
        return outputs

    def get_config(self):
        conf = super(ConformerBlock, self).get_config()
        conf.update(self.ffm1.get_config())
        conf.update(self.mhsam.get_config())
        conf.update(self.convm.get_config())
        conf.update(self.ffm2.get_config())
        conf.update(self.ln.get_config())
        return conf


class ConformerEncoder(tf.keras.Model):
    def __init__(self,
                 dmodel=144,
                 reduction_factor=4,
                 num_blocks=16,
                 head_size=36,
                 num_heads=4,
                 kernel_size=32,
                 fc_factor=0.5,
                 dropout=0.0,
                 add_wav_info=False,
                 sample_rate=16000,
                 n_mels=80,
                 mel_layer_type='leaf',
                 mel_layer_trainable=False,
                 stride_ms=10,
                 name="conformer_encoder",
                 **kwargs):
        super(ConformerEncoder, self).__init__(name=name, **kwargs)
        self.dmodel=dmodel
        self.num_heads=num_heads
        self.fc_factor=fc_factor
        self.dropout=dropout
        self.head_size=head_size
        self.hop_size=int(stride_ms * sample_rate // 1000)*reduction_factor
        self.add_wav_info = add_wav_info
        self.reduction_factor=reduction_factor
        self.conv_subsampling = ConvSubsampling(
            odim=dmodel, reduction_factor=reduction_factor,
            dropout=dropout
        )

        if mel_layer_type == 'Melspectrogram':
            self.mel_layer = Melspectrogram(sr=sample_rate, n_mels=n_mels,
                                            n_hop=int(stride_ms * sample_rate // 1000),
                                            n_dft=1024,
                                            trainable_fb=mel_layer_trainable
                                            )
        elif mel_layer_type=='leaf':
            self.mel_layer=frontend.Leaf(n_filters=n_mels,sample_rate=sample_rate,window_stride=stride_ms,complex_conv_init=frontend.initializers.GaborInit(sample_rate=sample_rate,min_freq=30*(sample_rate//800),
                                                                                                                                           max_freq=3900*(sample_rate//8000)))
        else:
            self.mel_layer = Spectrogram(
                n_hop=int(stride_ms* sample_rate// 1000),
                n_dft=1024,
                trainable_kernel=mel_layer_trainable
            )
        self.mel_layer.trainable =mel_layer_trainable
        if self.add_wav_info:
            self.wav_layer=WavePickModel(dmodel,self.hop_size)
        self.conformer_blocks = []
        for i in range(num_blocks):
            conformer_block = ConformerBlock(
                input_dim=dmodel,
                dropout=dropout,
                fc_factor=fc_factor,
                head_size=head_size,
                num_heads=num_heads,
                kernel_size=kernel_size,
                name=f"conformer_block_{i}"
            )
            self.conformer_blocks.append(conformer_block)
    def _build(self):
        fake=tf.random.uniform([1,16000,1])
        self(fake)

    def call(self, inputs, training=False, **kwargs):
        if self.add_wav_info:
            mel_inputs=self.mel_layer(inputs,training=training)
            mel_outputs = self.conv_subsampling(mel_inputs, training=training)
            wav_outputs = self.wav_layer(inputs, training=training)
            outputs = mel_outputs+wav_outputs
        else:
            inputs=self.mel_layer(inputs,training=training)
            outputs = self.conv_subsampling(inputs, training=training)

        for cblock in self.conformer_blocks:
            outputs = cblock(outputs, training=training)

        return outputs

    @tf.function(experimental_relax_shapes=True,
                 input_signature=[
                     tf.TensorSpec([None, None, 1], dtype=tf.int32),
                 ]
                 )
    def inference(self,inputs):
        if self.add_wav_info:
            mel_inputs=self.mel_layer(inputs)
            mel_outputs = self.conv_subsampling(mel_inputs, training=False)
            wav_outputs = self.wav_layer(inputs, training=False)
            outputs = mel_outputs+wav_outputs
        else:
            inputs=self.mel_layer(inputs)
            outputs = self.conv_subsampling(inputs, training=False)

        for cblock in self.conformer_blocks:
            outputs = cblock(outputs, training=False)

        return outputs
    def get_config(self):
        conf = super(ConformerEncoder, self).get_config()
        conf.update(self.conv_subsampling.get_config())
        for cblock in self.conformer_blocks:
            conf.update(cblock.get_config())
        return conf
class CTCDecoder(tf.keras.Model):
    def __init__(self,num_classes,
                 dmodel=144,
                 num_blocks=16,
                 head_size=36,
                 num_heads=4,
                 fc_factor=0.5,
                 dropout=0.0,
                 kernel_size=32,
                 **kwargs
                 ):
        super(CTCDecoder, self).__init__()
        self.decode_layers = []
        self.dmodel=dmodel
        self.project=WeightNormalization(tf.keras.layers.Dense(dmodel))
        for i in range(num_blocks):
            conformer_block = ConformerBlock(
                input_dim=dmodel,
                dropout=dropout,
                fc_factor=fc_factor,
                head_size=head_size,
                num_heads=num_heads,
                kernel_size=kernel_size,
                name=f"decoder_conformer_block_{i}"
            )
            self.decode_layers.append(conformer_block)

        self.fc = tf.keras.layers.Dense(units=num_classes, activation="linear",
                                  use_bias=True, name="fully_connected")

    def _build(self):
        fake=tf.random.uniform([1,100,self.dmodel])
        self(fake)
    # @tf.function(experimental_relax_shapes=True)
    def call(self, inputs, training=None, mask=None):
        outputs=self.project(inputs,training=training)
        for layer in self.decode_layers:
            outputs = layer(outputs, training=training)
        outputs = self.fc(outputs, training=training)
        return outputs

    @tf.function(experimental_relax_shapes=True,
                 input_signature=[
                     tf.TensorSpec([None, None,144], dtype=tf.int32),
                 ]
                 )
    def inference(self,inputs):
        outputs = self.project(inputs, training=False)
        for layer in self.decode_layers:
            outputs = layer(outputs, training=False)
        outputs = self.fc(outputs, training=False)
        return outputs
class RMHSAModule(tf.keras.layers.Layer):
    def __init__(self,
                 head_size,
                 num_heads,
                 dropout=0.0,
                 name="mhsa_module",
                 **kwargs):
        super(RMHSAModule, self).__init__(name=name, **kwargs)
        self.pc = PositionalEncoding()
        self.ln = tf.keras.layers.LayerNormalization()
        self.mha = MultiHeadAttention(head_size=head_size, num_heads=num_heads)
        self.do = tf.keras.layers.Dropout(dropout)
        self.res_add = tf.keras.layers.Add()


    # @tf.function(experimental_relax_shapes=True)
    def call(self, inputs,enc, training=False, **kwargs):
        outputs = self.pc(inputs)
        outputs = self.ln(outputs, training=training)
        # print(outputs.shape)
        outputs = self.mha([outputs, enc, enc], training=training)
        # print(outputs.shape)
        outputs = self.do(outputs, training=training)
        outputs = self.res_add([inputs, outputs])
        return outputs

    def get_config(self):
        conf = super(RMHSAModule, self).get_config()
        conf.update(self.pc.get_config())
        conf.update(self.ln.get_config())
        conf.update(self.mha.get_config())
        conf.update(self.do.get_config())
        conf.update(self.res_add.get_config())
        return conf
class RBlock(tf.keras.layers.Layer):
    def __init__(self,input_dim,
                 dropout=0.0,
                 fc_factor=0.5,
                 head_size=144,
                 num_heads=4,
                 kernel_size=32,
                 name="RBlock",
                 **kwargs):
        super(RBlock, self).__init__(name=name)
        self.ffm1 = FFModule(input_dim=input_dim,
                             dropout=dropout, fc_factor=fc_factor,
                             name="ff_module_1")
        self.mhsam = RMHSAModule(head_size=head_size, num_heads=num_heads,
                                dropout=dropout)
        self.convm = SEModule(filters=input_dim, kernel_size=kernel_size,
                                dropout=dropout)
        self.ffm2 = FFModule(input_dim=input_dim,
                             dropout=dropout, fc_factor=fc_factor,
                             name="ff_module_2")
        self.ln = tf.keras.layers.LayerNormalization()

    def call(self, inputs, enc,training=False, **kwargs):
        outputs = self.ffm1(inputs, training=training)
        outputs = self.mhsam(outputs,enc, training=training)
        outputs = self.convm(outputs, training=training)
        outputs = self.ffm2(outputs, training=training)
        outputs = self.ln(outputs, training=training)
        return outputs

    def get_config(self):
        conf = super(RBlock, self).get_config()
        conf.update(self.ffm1.get_config())
        conf.update(self.mhsam.get_config())
        conf.update(self.convm.get_config())
        conf.update(self.ffm2.get_config())
        conf.update(self.ln.get_config())
        return conf
class Translator(tf.keras.Model):
    def __init__(self,inp_classes,
                 tar_classes,
                 dmodel=144,
                 num_blocks=16,
                 head_size=36,
                 num_heads=4,
                 fc_factor=0.5,
                 dropout=0.0,
                 kernel_size=32,
                 **kwargs):
        super(Translator, self).__init__()
        self.dmodel = dmodel
        self.decode_layers=[]
        for i in range(num_blocks):
            r_block = RBlock(
                input_dim=dmodel,
                dropout=dropout,
                fc_factor=fc_factor,
                head_size=head_size,
                num_heads=num_heads,
                kernel_size=kernel_size,
                name=f"decoder_conformer_block_{i}"
            )
            self.decode_layers.append(r_block)
        self.inp_embedding=tf.keras.layers.Embedding(inp_classes,dmodel)
        self.fc = tf.keras.layers.Dense(units=tar_classes, activation="linear",
                                        use_bias=True, name="fully_connected")

    def _build(self):
        fake_a = tf.constant([[1, 2, 3, 4, 5, 6, 7]], tf.int32)
        fake_b = tf.random.uniform([1, 100, self.dmodel])
        self(fake_a,fake_b)


    def call(self, inputs,enc, training=None, mask=None):
        outputs=self.inp_embedding(inputs,training=training)
        for layer in self.decode_layers:
            outputs=layer(outputs,enc,training=training)
        outputs=self.fc(outputs,training=training)
        return outputs

    @tf.function(experimental_relax_shapes=True,
                 input_signature=[
                     tf.TensorSpec([None, None], dtype=tf.int32),
                     tf.TensorSpec([None, None, 256], dtype=tf.float32),#TODO:根据自己的dmodel修改
                 ]
                 )
    def inference(self,inputs,enc):
        outputs = self.inp_embedding(inputs, training=False)
        for layer in self.decode_layers:
            outputs = layer(outputs, enc, training=False)
        outputs = self.fc(outputs, training=False)
        return outputs
class StreamingConformerEncoder(ConformerEncoder):
    def add_chunk_size(self,chunk_size,mel_size,hop_size):
        self.chunk_size=chunk_size
        self.mel_size=mel_size
        self.mel_length=self.chunk_size//hop_size if self.chunk_size%hop_size==0 else self.chunk_size//hop_size+1
        print(self.chunk_size,self.mel_size,self.mel_length)

    def call(self, inputs, training=False, **kwargs):

        if self.add_wav_info:

            B = tf.shape(inputs)[0]

            inputs = tf.reshape(inputs, [-1, self.chunk_size, 1])
            mel_inputs=self.mel_layer(inputs)
            mel_outputs = self.conv_subsampling(mel_inputs, training=training)
            wav_outputs = self.wav_layer(inputs, training=training)
            outputs = mel_outputs + wav_outputs
        else:
            B=tf.shape(inputs)[0]
            inputs = tf.reshape(inputs, [-1, self.chunk_size, 1])
            inputs = self.mel_layer(inputs)
            outputs = self.conv_subsampling(inputs, training=training)

        for cblock in self.conformer_blocks:
            outputs = cblock(outputs, training=training)
        outputs = tf.reshape(outputs, [B, -1, self.dmodel])
        return outputs
    @tf.function(experimental_relax_shapes=True,
                 input_signature=[
                     tf.TensorSpec([None, None,1], dtype=tf.float32),
                 ]
                 )
    def inference(self, inputs, training=False, **kwargs):
        if self.add_wav_info:
            mel_inputs=self.mel_layer(inputs)
            mel_outputs = self.conv_subsampling(mel_inputs, training=training)
            wav_outputs = self.wav_layer(inputs, training=training)
            outputs = mel_outputs+wav_outputs
        else:
            inputs = self.mel_layer(inputs)
            outputs = self.conv_subsampling(inputs, training=training)

        for cblock in self.conformer_blocks:
            outputs = cblock(outputs, training=training)
        return outputs