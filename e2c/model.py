
import tensorflow as tf

from .input import get_constants, load_inverse_vocab

def basic_cell(args, i, unit_mul):

	c = tf.contrib.rnn.BasicLSTMCell(
		args['num_units']*unit_mul,
		forget_bias=args['forget_bias'])

	c = tf.contrib.rnn.DropoutWrapper(
		cell=c, input_keep_prob=(1.0 - args['dropout']))

	if i > 1:
		c = tf.contrib.rnn.ResidualWrapper(c)

	return c

def cell_stack(args, layer_mul=1, unit_mul=1):
	cells = []
	for i in range(args["num_layers"]*layer_mul):
		cells.append(basic_cell(args, i, unit_mul))

	cell = tf.contrib.rnn.MultiRNNCell(cells)
	return cell


def model_fn(features, labels, mode, params):

	# --------------------------------------------------------------------------
	# Hyper parameters
	# --------------------------------------------------------------------------
	
	# For consistency with rest of codebase
	args = params

	dtype = tf.float32
	beam_width = 0
	time_major = True
	vocab_const = get_constants(args)
	dynamic_batch_size = tf.shape(features["src"])[0]

	# --------------------------------------------------------------------------
	# Format inputs
	# --------------------------------------------------------------------------

	# embed words
	word_embedding = tf.get_variable("word_embedding", [args["vocab_size"], args["num_units"]], tf.float32)

	# Transpose to switch to time-major format [max_time, batch, ...]
	src  = tf.nn.embedding_lookup(word_embedding, tf.transpose(features["src"]))

	max_time = src.shape[0].value
	


	# --------------------------------------------------------------------------
	# Encoder
	# --------------------------------------------------------------------------
	
	fw_cell = cell_stack(args)
	bw_cell = cell_stack(args)

	
	# delta = tf.shape(src)[1] - tf.shape(features["src_len"])[0]
	# tf.pad(features["src_len"], [[0, delta]], 'REFLECT')

	# Trim down to the residual batch size (e.g. when at end of input data)
	padded_src_len = features["src_len"][0 : dynamic_batch_size]
	

	(fw_output, bw_output), (fw_states, bw_states) = tf.nn.bidirectional_dynamic_rnn(
        fw_cell,
        bw_cell,
        src,
        dtype=dtype,
        sequence_length=padded_src_len,
        time_major=time_major,
        swap_memory=True)

	encoder_outputs = tf.concat( (fw_output, bw_output), axis=-1)
	
	# Interleave-stack the forward and backward layers
	# Note: this doubles the number of layers w.r.t (e.g. for the decoder to handle)
	encoder_state = []
	for layer_id in range(args["num_layers"]):
		encoder_state.append(fw_states[layer_id])  # forward
		encoder_state.append(bw_states[layer_id])  # backward
	encoder_state = tuple(encoder_state)

	# e_initial_state = e_cell.zero_state(args["batch_size"], dtype)
	# 'outputs' is a tensor of shape [max_time, batch_size, cell.output_size]
	# 'state' is a tensor of shape [batch_size, cell_state_size]
	# inputs [max_time, batch_size, cell_state_size]
	# encoder_outputs, encoder_state = tf.nn.dynamic_rnn(
	# 		e_cell,
	# 		src,
	# 		initial_state = e_initial_state,
	# 		dtype=dtype,
	# 		sequence_length=features["src_len"],
	# 		time_major=True,
	# 		swap_memory=True)



	# --------------------------------------------------------------------------
	# Decoder base
	# --------------------------------------------------------------------------

	# 2x layers since input is a biLSTM
	d_cell = cell_stack(args, 2)

	# Time major formatting
	memory = tf.transpose(encoder_outputs, [1, 0, 2])
	
	# set up attention
	attention_mechanism = tf.contrib.seq2seq.BahdanauAttention(
		args["num_units"], memory, 
		memory_sequence_length=features["src_len"], 
		normalize=True)

	alignment_history = (mode == tf.contrib.learn.ModeKeys.INFER and
						 beam_width == 0)

	d_cell = tf.contrib.seq2seq.AttentionWrapper(
		d_cell,
		attention_mechanism,
		attention_layer_size=args["num_units"],
		alignment_history=alignment_history,
		output_attention=True,
		name="attention")

	d_initial_state = d_cell.zero_state(dynamic_batch_size, dtype).clone(
		cell_state=encoder_state)

	output_layer = tf.layers.Dense(
		args["vocab_size"], 
		use_bias=False, name="output_projection")

	

	if mode in [tf.estimator.ModeKeys.TRAIN, tf.estimator.ModeKeys.EVAL]:

		tgt_in  = tf.nn.embedding_lookup(word_embedding, tf.transpose(features["tgt_in"]))

		decoder_helper = tf.contrib.seq2seq.TrainingHelper(
				tgt_in, features["tgt_len"],
				time_major=time_major)

		maximum_iterations = None

		guided_decoder = tf.contrib.seq2seq.BasicDecoder(
			d_cell,
			decoder_helper,
			d_initial_state,)

		# 'outputs' is a tensor of shape [max_time, batch_size, cell.output_size]
		guided_decoded, _, _ = tf.contrib.seq2seq.dynamic_decode(
			guided_decoder,
			output_time_major=time_major,
			swap_memory=True,
			maximum_iterations=maximum_iterations)

		guided_logits = output_layer(guided_decoded.rnn_output)

	if mode in [tf.estimator.ModeKeys.PREDICT, tf.estimator.ModeKeys.EVAL]:

		decoder_helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(
			word_embedding,
			tf.fill([dynamic_batch_size], vocab_const['tgt_sos_id']), vocab_const['tgt_eos_id'])

		maximum_iterations = tf.round(tf.reduce_max(features["src_len"]) * 2)

		free_decoder = tf.contrib.seq2seq.BasicDecoder(
			d_cell,
			decoder_helper,
			d_initial_state,)

		free_decoded, _, _ = tf.contrib.seq2seq.dynamic_decode(
			free_decoder,
			output_time_major=time_major,
			swap_memory=True,
			maximum_iterations=maximum_iterations)

		free_logits = output_layer(free_decoded.rnn_output)
		free_predictions = tf.argmax(free_logits, axis=-1)

	


	if mode in [tf.estimator.ModeKeys.TRAIN, tf.estimator.ModeKeys.EVAL]:
		# Time major formatting
		labels_t = tf.transpose(labels)

		# Mask of the outputs we care about
		target_weights = tf.sequence_mask(
			features["tgt_len"], max_time, dtype=guided_logits.dtype)

		# Time major formatting
		target_weights = tf.transpose(target_weights)

		# --------------------------------------------------------------------------
		# Calc loss
		# --------------------------------------------------------------------------	
		
		crossent = tf.nn.sparse_softmax_cross_entropy_with_logits(
			labels=labels_t, logits=guided_logits)

		loss = tf.reduce_sum(crossent * target_weights) / tf.to_float(dynamic_batch_size)

	else: 
		loss = None

	# --------------------------------------------------------------------------
	# Optimize
	# --------------------------------------------------------------------------

	if mode == tf.estimator.ModeKeys.TRAIN:

		var = tf.trainable_variables()
		gradients = tf.gradients(loss, var)
		clipped_gradients, _ = tf.clip_by_global_norm(gradients, args["max_gradient_norm"])
		
		optimizer = tf.train.AdamOptimizer(args["learning_rate"])
		train_op = optimizer.apply_gradients(zip(clipped_gradients, var), global_step=tf.train.get_global_step())

	else:
		train_op = None

	# --------------------------------------------------------------------------
	# Eval
	# --------------------------------------------------------------------------

	if mode == tf.estimator.ModeKeys.EVAL:

		delta = tf.shape(labels_t)[0] - tf.shape(free_predictions)[0]
		
		padded_free_predictions = tf.pad(free_predictions, 
			[ [0,delta], [0,0] ], 
			constant_values=tf.cast(vocab_const['tgt_eos_id'], tf.int64)
		)

		eval_metric_ops = {
			"guided_accuracy": tf.metrics.accuracy(
				labels=labels_t, 
				predictions=tf.argmax(guided_logits, axis=-1),
				weights=target_weights
			),
			"free_accuracy": tf.metrics.accuracy(
				labels=labels_t, 
				predictions=padded_free_predictions,
				weights=target_weights
			),
		}

	else:
		eval_metric_ops = None

	# --------------------------------------------------------------------------
	# Predictions
	# --------------------------------------------------------------------------

	if mode == tf.estimator.ModeKeys.PREDICT:

		vocab_inverse = load_inverse_vocab(args)
		predictions = vocab_inverse.lookup(free_predictions)

	else:
		predictions = None
	

	return tf.estimator.EstimatorSpec(mode,
		loss=loss,
		train_op=train_op,
		predictions=predictions,
		eval_metric_ops=eval_metric_ops,
		export_outputs=None,
		training_chief_hooks=None,
		training_hooks=None,
		scaffold=None,
		evaluation_hooks=None,
		prediction_hooks=None
	)



	