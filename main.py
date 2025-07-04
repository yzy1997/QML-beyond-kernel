import os, pickle, sys
os.environ["CUDA_VISIBLE_DEVICES"] = "3"
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
import tensorflow as tf
import tensorflow_quantum as tfq
import cirq, sympy, scipy
import numpy as np
import sklearn.kernel_ridge, sklearn.metrics.pairwise

# Helper functions to generate the quantum circuits

# 在导入 tensorflow 后添加
gpus = tf.config.experimental.list_physical_devices('GPU')
if gpus:
    try:
        for gpu in gpus:
            tf.config.experimental.set_memory_growth(gpu, True)
    except RuntimeError as e:
        print(e)

def one_qubit_rotation(qubit, symbols):
	"""Arbitrary single-qubit rotation"""
	return [cirq.rx(symbols[0])(qubit),
			cirq.ry(symbols[1])(qubit),
			cirq.rz(symbols[2])(qubit)]

def entangling_layer(qubits):
	"""A layer of nearest-neighbour CZ gates, with circular boundary conditions"""
	cz_ops = [cirq.CZ(q0, q1) for q0, q1 in zip(qubits, qubits[1:])]
	cz_ops += ([cirq.CZ(qubits[0], qubits[-1])] if len(qubits) != 2 else [])
	return cz_ops

def generate_encoding(qubits):
	"""Implements Havlivcek's IQP-type encoding"""
	n_qubits = len(qubits)

	# Sympy symbols for encoding angles
	inputs = sympy.symbols(f'x(0:{n_qubits})')
	inputs_prod = sympy.symbols(f'y(0:{n_qubits * (n_qubits - 1) // 2})')

	# Define circuit
	circuit = cirq.Circuit()

	# Encoding gates
	for k in range(2):
		circuit += cirq.Circuit(cirq.H(q) for i, q in enumerate(qubits))
		circuit += cirq.Circuit(cirq.ZPowGate(exponent=inputs[i])(q) for i, q in enumerate(qubits))
		count = 0
		for i in range(n_qubits):
			for j in range(i + 1, n_qubits):
				circuit += cirq.Circuit(cirq.ops.ZZPowGate(exponent=inputs_prod[count])(qubits[i], qubits[j]))
				count += 1

	return circuit, inputs, inputs_prod

def generate_circuit(qubits, n_layers, heisenberg=False):
	"""Implements the circuits for the explicit models using Havlivcek's IQP-type encoding, and n_layers of a hardware-efficient variational unitary. When heisenberg = True, a variational Trotter evolution of a 1D-Heisenberg model is used instead."""
	# Number of qubits
	n_qubits = len(qubits)

	# Sympy symbols for variational angles
	params = sympy.symbols(f'theta(0:{3 * n_layers * n_qubits})')
	params = np.asarray(params).reshape((n_layers, n_qubits, 3))

	# Define circuit
	circuit = cirq.Circuit()

	# Encoding layer
	enc_circuit, inputs, inputs_prod = generate_encoding(qubits)
	circuit += enc_circuit

	# Variational layers
	if heisenberg:
		for l in range(n_layers):
			for i in range(len(qubits)):
				circuit += cirq.Circuit(tfq.util.exponential([cirq.X(qubits[i]) * cirq.X(
							qubits[(i + 1) % len(qubits)]), cirq.Y(qubits[i]) * cirq.Y(
							qubits[(i + 1) % len(qubits)]), cirq.Z(qubits[i]) * cirq.Z(qubits[(i + 1) % len(qubits)])], params[l, i]))
	else:
		for l in range(n_layers - 1):
			circuit += cirq.Circuit(one_qubit_rotation(q, params[l, i]) for i, q in enumerate(qubits))
			circuit += entangling_layer(qubits)
		# Last variational layer
		circuit += cirq.Circuit(one_qubit_rotation(q, params[n_layers - 1, i]) for i, q in enumerate(qubits))

	return circuit, list(params.flat), list(inputs), list(inputs_prod)

class ExplicitPQC(tf.keras.layers.Layer):
    """The Keras layer used by the explicit model to store its circuit, parameters, and evaluate itself."""
    def __init__(self, qubits, n_layers, observables, train, heisenberg, name="explicit_PQC"):
        super(ExplicitPQC, self).__init__(name=name)
        self.n_layers = n_layers
        self.n_qubits = len(qubits)
        self.enc_layers = 1

        circuit, theta_symbols, input_symbols, inputs_prod = generate_circuit(qubits, n_layers, heisenberg)

        if train:
            theta_init = tf.random_normal_initializer(mean=0.0, stddev=0.05)
        else:
            theta_init = tf.random_uniform_initializer(minval=0.0, maxval=np.pi)
            
        # 使用 add_weight 创建变量
        self.theta = self.add_weight(
            name="thetas",
            shape=(1, len(theta_symbols)),
            initializer=theta_init,
            dtype="float32",
            trainable=True
        )

        # 定义符号顺序
        symbols = [str(symb) for symb in theta_symbols + input_symbols + inputs_prod]
        self.indices = tf.constant([symbols.index(a) for a in sorted(symbols)])

        # 保存电路定义
        self.circuit_definition = circuit
        self.observables = observables
        
        # 延迟初始化
        self.empty_circuit = None
        self.computation_layer = None

    def build(self, input_shape):
        # 延迟创建计算层
        self.empty_circuit = tfq.convert_to_tensor([cirq.Circuit()])
        self.computation_layer = tfq.layers.ControlledPQC(self.circuit_definition, self.observables)
        super().build(input_shape)

    def call(self, inputs):
        # 确保计算层已创建
        if self.computation_layer is None:
            self.build(inputs[0].shape)
        
        batch_dim = tf.gather(tf.shape(inputs[0]), 0)
        tiled_up_circuits = tf.repeat(self.empty_circuit, repeats=batch_dim)
        tiled_up_thetas = tf.tile(self.theta, multiples=[batch_dim, 1])
        tiled_up_inputs = tf.tile(inputs[0], multiples=[1, self.enc_layers])
        joined_vars = tf.concat([tiled_up_thetas, tiled_up_inputs], axis=1)
        joined_vars = tf.gather(joined_vars, self.indices, axis=1)

        return self.computation_layer([tiled_up_circuits, joined_vars])

class Rescaling(tf.keras.layers.Layer):
	"""A post-processing layer to rescale the explicit model's expectation values with a trainable weight."""
	def __init__(self, input_dim):
		super(Rescaling, self).__init__()
		self.input_dim = input_dim
		self.w = tf.Variable(
			initial_value=tf.ones(shape=(1,input_dim)), dtype="float32",
			trainable=True, name="obs-weights")

	def call(self, inputs):
		return tf.math.multiply(inputs, tf.repeat(self.w, repeats=tf.shape(inputs)[0], axis=0))

# Implicit and explicit models

class Implicit:

	def __init__(self, qubits):

		circuit, input_symbols, inputs_prod = generate_encoding(qubits)
		self.symbol_names = [str(symb) for symb in list(input_symbols) + list(inputs_prod)]

		self.circuit = tfq.convert_to_tensor([circuit])
		self.kernel = None
		self.d = None
		self.g = None

	def kernel_eval(self, x, xp):
		"""Not used. Evaluates the kernel function for two input vectors x, xp."""
		feature_state = tfq.resolve_parameters(self.circuit, self.symbol_names, x)
		inner_product = tfq.math.inner_product(self.circuit, self.symbol_names, xp, [feature_state])

		return (np.abs(inner_product[0]) ** 2)[0]

	# def kernel_matrix(self, X, Xp):
	# 	"""Evaluates the kernel matrix between two data matrices X, Xp (each storing single vectors in rows)."""
	# 	gramm = []
	# 	circuits = tf.repeat(self.circuit, repeats=len(Xp))
	# 	feature_states = tfq.resolve_parameters(circuits, self.symbol_names, Xp)
	# 	# For training, X == Xp
	# 	if np.all(X == Xp):
	# 		for i, x in enumerate(X):
	# 			inner_products = tfq.math.inner_product(self.circuit, self.symbol_names, [x],
	# 													[tf.gather(feature_states, np.arange(i + 1, len(X)))])
	# 			gramm += [np.concatenate([[0] * i + [1], np.abs(inner_products)[0] ** 2])]
	# 		for i in range(len(X)):
	# 			for j in range(i + 1, len(X)):
	# 				gramm[j][i] = gramm[i][j]
	# 	# For model evaluation, X != Xp
	# 	else:
	# 		for x in X:
	# 			inner_products = tfq.math.inner_product(self.circuit, self.symbol_names, [x], [feature_states])
	# 			gramm += [np.abs(inner_products)[0] ** 2]
	# 	return np.array(gramm, dtype=np.float32)
	def kernel_matrix(self, X, Xp):
		"""Evaluates the kernel matrix between two data matrices X, Xp (each storing single vectors in rows)."""
		gramm = []
		circuits = tf.repeat(self.circuit, repeats=len(Xp))
		feature_states = tfq.resolve_parameters(circuits, self.symbol_names, Xp)
		
		# 更高效的比较方式
		same_data = False
		if X.shape == Xp.shape:
			# 只比较第一个元素作为快速检查
			if tf.reduce_all(tf.equal(X[0], Xp[0])):
				# 如果第一个元素相同，再比较最后一个元素
				if tf.reduce_all(tf.equal(X[-1], Xp[-1])):
					same_data = True
		
		# 使用 TensorFlow 的 reduce_all 而不是 np.all
		if same_data:
			for i, x in enumerate(X):
				# 只计算上三角部分 - 修复这里的括号问题
				inner_products = tfq.math.inner_product(
					self.circuit, 
					self.symbol_names, 
					[x],
					[tf.gather(feature_states, np.arange(i + 1, len(X)))]
				)
				row = np.concatenate([[0] * i + [1], np.abs(inner_products)[0] ** 2])
				gramm.append(row)
			
			# 填充下三角部分
			gramm = np.array(gramm)
			for i in range(len(X)):
				for j in range(i + 1, len(X)):
					gramm[j][i] = gramm[i][j]
		else:
			for x in X:
				inner_products = tfq.math.inner_product(
					self.circuit, 
					self.symbol_names, 
					[x], 
					[feature_states]
				)
				gramm.append(np.abs(inner_products)[0] ** 2)
		
		return np.array(gramm, dtype=np.float32)

class Explicit:

	# def __init__(self, qubits, n_layers, observables, train=False, heisenberg=False):
	# 	self.qubits = qubits
	# 	self.n_layers = n_layers
	# 	self.observables = observables
	# 	self.heisenberg = heisenberg
	# 	self.x_train, self.y_train, self.x_test, self.y_test, self.x_train_save, self.x_test_save = None, None, None, None, None, None
	# 	self.std = None
	# 	self.model = self.generate_model_explicit(train, heisenberg)
	# 	self.variables = self.model.variables
	# 	self.optimizer_var = tf.keras.optimizers.Adam(learning_rate=0.01, amsgrad=True)
	# 	self.optimizer_out = tf.keras.optimizers.Adam(learning_rate=0.1, amsgrad=True)
	# 	self.loss_history = []
	# 	self.val_history = []
	# 	self.test_history = []
 
	def __init__(self, qubits, n_layers, observables, train=False, heisenberg=False):
		self.qubits = qubits
		self.n_layers = n_layers
		self.observables = observables
		self.heisenberg = heisenberg
		self.x_train, self.y_train, self.x_test, self.y_test, self.x_test2, self.y_test2 = None, None, None, None, None, None
		self.x_train_save, self.x_test_save, self.x_test2_save = None, None, None

		# 初始化10维数据属性
		self.x_train_10 = None
		self.x_test_10 = None
		self.x_test2_10 = None

		self.std = None
		self.model = self.generate_model_explicit(train, heisenberg)
		if self.model is not None:  # 确保模型已创建
			self.variables = self.model.variables
		else:
			self.variables = None
		self.optimizer_var = tf.keras.optimizers.Adam(learning_rate=0.01, amsgrad=True)
		self.optimizer_out = tf.keras.optimizers.Adam(learning_rate=0.1, amsgrad=True)
		self.loss_history = []
		self.val_history = []
		self.test_history = []		
		return

	def generate_model_explicit(self, train=False, heisenberg=False):
		"""Generates the explicit model."""
		input_tensor = tf.keras.Input(shape=(len(self.qubits),), dtype=tf.dtypes.float32, name='input')
		explicit_pqc = ExplicitPQC(self.qubits, self.n_layers, self.observables, train, heisenberg)([input_tensor])
		if train:
			explicit_pqc = Rescaling(1)(explicit_pqc)
		model = tf.keras.Model(inputs=[input_tensor], outputs=explicit_pqc)

		return model

	# def generate_fMNIST(self, nb_train, nb_test, norm):
	# 	"""Generates the pre-process fashion MNIST dataset."""
	# 	# Load raw dataset
	# 	(x_train, y_train), (x_test, y_test) = tf.keras.datasets.fashion_mnist.load_data()
	# 	x_train, x_test = x_train / 255.0, x_test / 255.0

	# 	# PCA and component-wise normalization
	# 	x_train, x_test = truncate_x(x_train, x_test, n_components=n_qubits)
	# 	x_mean, x_std = np.mean(x_train, axis=0), np.std(x_train, axis=0)
	# 	x_train, x_test = (x_train - x_mean) / x_std, (x_test - x_mean) / x_std

	# 	# Prune dataset
	# 	x_train, x_test, x_test2 = x_train[:nb_train], x_test[:nb_test], x_test[nb_test:2*nb_test]

	# 	# _save data for classical methods and compute the feature vectors of Havlivcek's encoding
	# 	x_train_save, x_test_save, x_test2_save = x_train.numpy(), x_test.numpy(), x_test2.numpy()
	# 	x_train, x_test, x_test2 = tf.convert_to_tensor(preprocess(x_train.numpy())), tf.convert_to_tensor(preprocess(x_test.numpy())), tf.convert_to_tensor(preprocess(x_test2.numpy()))

	# 	# Compute new labels and normalize
	# 	y_train, y_test, y_test2 = self.model(x_train), self.model(x_test), self.model(x_test2)
	# 	if norm:
	# 		std = np.std(y_train)
	# 		self.std = std
	# 		y_train, y_test, y_test2 = y_train/std, y_test/std, y_test2/std

	# 	self.x_train, self.y_train, self.x_test, self.y_test, self.x_test2, self.y_test2, self.x_train_save, self.x_test_save, self.x_test2_save  = x_train, y_train, x_test, y_test, x_test2, y_test2, x_train_save, x_test_save, x_test2_save

	# 	return x_train, y_train, x_test, y_test, x_test2, y_test2, x_train_save, x_test_save, x_test2_save
	# def generate_fMNIST(self, nb_train, nb_test, norm):
	# 	"""Generates the pre-process fashion MNIST dataset."""
	# 	# Load raw dataset
	# 	(x_train, y_train), (x_test, y_test) = tf.keras.datasets.fashion_mnist.load_data()
	# 	x_train, x_test = x_train / 255.0, x_test / 255.0

	# 	# PCA and component-wise normalization
	# 	x_train, x_test = truncate_x(x_train, x_test, n_components=n_qubits)
	# 	x_mean, x_std = np.mean(x_train, axis=0), np.std(x_train, axis=0)
	# 	x_train, x_test = (x_train - x_mean) / x_std, (x_test - x_mean) / x_std

	# 	# Prune dataset
	# 	x_train, x_test, x_test2 = x_train[:nb_train], x_test[:nb_test], x_test[nb_test:2*nb_test]

	# 	# 保存原始10维数据
	# 	x_train_10 = x_train.numpy()
	# 	x_test_10 = x_test.numpy()
	# 	x_test2_10 = x_test2.numpy()
		
	# 	# _save data for classical methods (55维)
	# 	x_train_save = preprocess(x_train_10)
	# 	x_test_save = preprocess(x_test_10)
	# 	x_test2_save = preprocess(x_test2_10)
		
	# 	# 生成标签使用原始10维数据
	# 	y_train = self.model(tf.convert_to_tensor(x_train_10))
	# 	y_test = self.model(tf.convert_to_tensor(x_test_10))
	# 	y_test2 = self.model(tf.convert_to_tensor(x_test2_10))
		
	# 	if norm:
	# 		std = np.std(y_train)
	# 		self.std = std
	# 		y_train, y_test, y_test2 = y_train/std, y_test/std, y_test2/std

	# 	# 保存所有数据
	# 	self.x_train = tf.convert_to_tensor(x_train_save)
	# 	self.y_train = y_train
	# 	self.x_test = tf.convert_to_tensor(x_test_save)
	# 	self.y_test = y_test
	# 	self.x_test2 = tf.convert_to_tensor(x_test2_save)
	# 	self.y_test2 = y_test2
	# 	self.x_train_save = x_train_save
	# 	self.x_test_save = x_test_save
	# 	self.x_test2_save = x_test2_save
	# 	self.x_train_10 = x_train_10
	# 	self.x_test_10 = x_test_10
	# 	self.x_test2_10 = x_test2_10

	# 	return (self.x_train, self.y_train, self.x_test, self.y_test, 
	# 			self.x_test2, self.y_test2, self.x_train_save, 
	# 			self.x_test_save, self.x_test2_save)
 
	def generate_fMNIST(self, nb_train, nb_test, norm):
		"""Generates the pre-process fashion MNIST dataset."""
		# Load raw dataset
		(x_train, y_train), (x_test, y_test) = tf.keras.datasets.fashion_mnist.load_data()
		x_train, x_test = x_train / 255.0, x_test / 255.0

		# PCA and component-wise normalization
		x_train, x_test = truncate_x(x_train, x_test, n_components=n_qubits)
		x_mean, x_std = np.mean(x_train, axis=0), np.std(x_train, axis=0)
		x_train, x_test = (x_train - x_mean) / x_std, (x_test - x_mean) / x_std

		# Prune dataset
		x_train, x_test, x_test2 = x_train[:nb_train], x_test[:nb_test], x_test[nb_test:2*nb_test]

		# 保存原始10维数据
		x_train_10 = x_train.numpy()
		x_test_10 = x_test.numpy()
		x_test2_10 = x_test2.numpy()
		
		# _save data for classical methods (55维)
		x_train_save = preprocess(x_train_10)
		x_test_save = preprocess(x_test_10)
		x_test2_save = preprocess(x_test2_10)
		
		# 生成标签使用原始10维数据
		# 确保模型已初始化
		if self.model is None:
			self.model = self.generate_model_explicit(False, self.heisenberg)
		
		y_train = self.model(tf.convert_to_tensor(x_train_10))
		y_test = self.model(tf.convert_to_tensor(x_test_10))
		y_test2 = self.model(tf.convert_to_tensor(x_test2_10))
		
		if norm:
			std = np.std(y_train)
			self.std = std
			y_train, y_test, y_test2 = y_train/std, y_test/std, y_test2/std

		# 保存所有数据
		self.x_train = tf.convert_to_tensor(x_train_save)
		self.y_train = y_train
		self.x_test = tf.convert_to_tensor(x_test_save)
		self.y_test = y_test
		self.x_test2 = tf.convert_to_tensor(x_test2_save)
		self.y_test2 = y_test2
		self.x_train_save = x_train_save
		self.x_test_save = x_test_save
		self.x_test2_save = x_test2_save
		
		# 保存10维数据
		self.x_train_10 = x_train_10
		self.x_test_10 = x_test_10
		self.x_test2_10 = x_test2_10

		return (self.x_train, self.y_train, self.x_test, self.y_test, 
				self.x_test2, self.y_test2, self.x_train_save, 
				self.x_test_save, self.x_test2_save)			

	def relabel(self, norm):
		"""Relabel data when this is not the first explicit model at a given system size that is generating the data."""
		x_train, x_test, x_test2 = self.x_train, self.x_test, self.x_test2

		y_train, y_test, y_test2 = self.model(x_train), self.model(x_test), self.model(x_test2)
		if norm:
			std = np.std(y_train)
			self.std = std
			y_train, y_test, y_test2 = y_train/std, y_test/std, y_test2/std
		self.y_train, self.y_test, self.y_test2 = y_train, y_test, y_test2

		return y_train, y_test, y_test2

	def learning_step(self, y_train, y_test, batchsize=None):
		"""One gradient descent step on the training loss."""
		model = self.model
		
		# 使用原始10维数据
		X_train = tf.convert_to_tensor(self.x_train_10)
		X_test = tf.convert_to_tensor(self.x_test_10)
		
		# Evaluate the labels assigned by the model and the resulting MSE loss
		if batchsize is None:
			X = X_train
			y = y_train
		else:
			indices = np.random.randint(len(y_train), size=batchsize)
			X = tf.gather(X_train, indices)
			y = tf.gather(y_train, indices)
		
		with tf.GradientTape() as tape:
			tape.watch(model.trainable_variables)
			output = model(X)
			loss = tf.keras.losses.MeanSquaredError()(output, y)

		# Backpropagation
		grads = tape.gradient(loss, model.trainable_variables)
		w_var, w_out = 0, 1
		for optimizer, w in zip([self.optimizer_var, self.optimizer_out], [w_var, w_out]):
			optimizer.apply_gradients([(grads[w], model.trainable_variables[w])])

		# Evaluate validation loss on test set
		val_loss = tf.keras.losses.MeanSquaredError()(model(X_test), y_test)

		self.loss_history += [loss.numpy()]
		self.val_history += [val_loss.numpy()]

		return loss.numpy(), val_loss.numpy()

# Helper functions for data pre-processing

def truncate_x(x_train, x_test, n_components):
	"""Perform PCA on image dataset, keeping the top n_components."""
	n_points_train = tf.gather(tf.shape(x_train), 0)
	n_points_test = tf.gather(tf.shape(x_test), 0)

	# Flatten to 1D
	x_train = tf.reshape(x_train, [n_points_train, -1])
	x_test = tf.reshape(x_test, [n_points_test, -1])

	# Normalize
	feature_mean = tf.reduce_mean(x_train, axis=0)
	x_train_normalized = x_train - feature_mean
	x_test_normalized = x_test - feature_mean

	# Truncate
	e_values, e_vectors = tf.linalg.eigh(
		tf.einsum('ji,jk->ik', x_train_normalized, x_train_normalized))
	return tf.einsum('ij,jk->ik', x_train_normalized, e_vectors[:,-n_components:]), tf.einsum('ij,jk->ik', x_test_normalized, e_vectors[:, -n_components:])

def preprocess(x):
	"""Computes the feature vectors of Havlivcek's encoding."""
	for i in range(n_qubits):
		for j in range(i+1, n_qubits):
			x = np.append(x, np.transpose([x[:,i]*x[:,j]]), axis=1)
	return x

# Helper functions for the implicit models

def mse(y_test, y):
	"""Computes the mean squared error on the test set."""
	loss = 0
	for i in range(len(y)):
		loss += (y[i] - y_test[i, 0]) ** 2
	return loss / len(y)

def compute_g(X, kernel):
	"""Computes geometric difference of quantum kernel with linear and gaussian kernels, as prescribed by Power of data in QML (Huang et al.)."""
	lmbds = [0.00001, 0.0001, 0.001, 0.01, 0.025, 0.05, 0.1]
	g_s = []
	kc = np.matmul(X, X.T)
	N = len(X)
	kc *= N / np.trace(kc)
	sqrt_kernel = scipy.linalg.sqrtm(kernel)
	sqrt_kc = scipy.linalg.sqrtm(kc)

	def lmbd_loop(g_s):
		for lmbd in lmbds:
			tmp = scipy.linalg.inv(kc + lmbd * np.eye(N))
			tmp_2 = np.matmul(tmp, tmp)
			matrix = np.matmul(np.matmul(sqrt_kernel, tmp_2), sqrt_kernel)
			S, V = tf.linalg.eigh(matrix)
			if lmbd*np.sqrt(np.max(tf.math.abs(S))) < 0.045:
				matrix = np.matmul(np.matmul(sqrt_kernel, np.matmul(np.matmul(sqrt_kc, tmp_2), sqrt_kc)), sqrt_kernel)
				S, V = tf.linalg.eigh(matrix)
				g_s += [np.sqrt(np.max(tf.math.abs(S)))]
		return g_s

	lmbd_loop(g_s)
	gammas = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0]
	gammas = np.array(gammas)/(n_qubits*np.std(X)**2)
	for gamma in gammas:
		kc = sklearn.metrics.pairwise.rbf_kernel(X, gamma=gamma)
		kc *= N / np.trace(kc)
		sqrt_kc = scipy.linalg.sqrtm(kc)
		lmbd_loop(g_s)
	return g_s

def compute_d(kernel):
	"""Computes effective dimension of quantum kernel, as prescribed by Power of data in QML (Huang et al.)."""
	S, V = tf.linalg.eigh(kernel)
	S = tf.sort(S)
	d = 0.
	sum = 0.
	for i in range(len(S)):
		sum += S[i].numpy()
		d += sum/(i+1)
	return d



if __name__ == '__main__':
	sys_args = sys.argv

	# 参数解析
	n_qubits = int(sys_args[1])
	n_layers = int(sys_args[2])
	nb_train = int(sys_args[3])
	is_first_run = str(sys_args[4]) == '0'
	heisenberg = (str(sys_args[5]) == 'True')
	nb_test = 100
	norm = True	
	qubits = cirq.GridQubit.rect(1, n_qubits)
	observables = [cirq.Z(qubits[0])]	
	# 创建两个 Explicit 对象
	gen = Explicit(qubits, n_layers, observables, heisenberg=heisenberg)
	trn = Explicit(qubits, n_layers, observables, train=True, heisenberg=heisenberg)	
	# First execution at this system size?
	if is_first_run:
		# 生成数据
		x_train, y_train, x_test, y_test, x_test2, y_test2, x_train_save, _ , _ = gen.generate_fMNIST(nb_train, nb_test, norm)

		# 将生成的数据复制到 trn 对象中
		trn.x_train_10 = gen.x_train_10
		trn.x_test_10 = gen.x_test_10
		trn.x_test2_10 = gen.x_test2_10
		trn.y_train = gen.y_train
		trn.y_test = gen.y_test
		trn.y_test2 = gen.y_test2

		# 计算 kernel
		impl = Implicit(qubits)
		kernel = impl.kernel_matrix(gen.x_train_save, gen.x_train_save)  # 使用55维数据

		# 计算有效维度和几何差异
		kernel_tensor = tf.convert_to_tensor(kernel)
		d = compute_d(kernel_tensor)
		print('Effective dimension d', d)

		g = compute_g(gen.x_train_save, kernel)  # 使用55维数据
		print('Geometric difference g', g)

		# 存储 kernel 矩阵
		impl.kernel = kernel
		impl.d = d
		impl.g = g
	else:
		# 加载之前保存的数据
		pickle_path = f'./results/n{str(sys_args[1])}_L{str(sys_args[2])}_T{str(sys_args[3])}_0_fashion'
		if heisenberg:
			pickle_path += '_heisen'
		pickle_path += 'gauss.pckl'

		l = pickle.load(open(pickle_path, 'rb'))
		impl = l[2]
		kernel = np.copy(impl.kernel)
		impl.kernel = None
		d = impl.d
		g = impl.g
		gen_old = l[0]

		# 将加载的数据复制到 gen 和 trn 对象
		gen.x_train_10 = gen_old.x_train_10
		gen.x_test_10 = gen_old.x_test_10
		gen.x_test2_10 = gen_old.x_test2_10
		gen.y_train = gen_old.y_train
		gen.y_test = gen_old.y_test
		gen.y_test2 = gen_old.y_test2

		trn.x_train_10 = gen_old.x_train_10
		trn.x_test_10 = gen_old.x_test_10
		trn.x_test2_10 = gen_old.x_test2_10
		trn.y_train = gen_old.y_train
		trn.y_test = gen_old.y_test
		trn.y_test2 = gen_old.y_test2

		# 重新标准化标签
		y_train, y_test, y_test2 = gen.relabel(norm)
		trn.y_train = y_train
		trn.y_test = y_test
		trn.y_test2 = y_test2

	# "Train" implicit model
	# First unregularized
	regr = sklearn.kernel_ridge.KernelRidge(kernel='precomputed', alpha=0.)
	regr.fit(kernel, np.array(y_train).flatten())
	err_0 = mse(y_train, regr.predict(kernel)).numpy()
	val_0 = mse(y_test, regr.predict(impl.kernel_matrix(x_test, x_train))).numpy()
	test_0 = mse(y_test2, regr.predict(impl.kernel_matrix(x_test2, x_train))).numpy()
	print('Unregularized training loss, validation loss, test loss', err_0, val_0, test_0)
	errs = [err_0]
	vals = [val_0]
	tests = [test_0]
	# Now regularized
	Cs = [0.006, 0.015, 0.03, 0.0625, 0.125, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256, 512, 1024]
	alphas = 1/(2*np.array(Cs[::-1]))
	for alpha in alphas:
		regr = sklearn.kernel_ridge.KernelRidge(kernel='precomputed', alpha=alpha)
		regr.fit(kernel, np.array(y_train).flatten())
		errs += [mse(y_train, regr.predict(kernel)).numpy()]
		vals += [mse(y_test, regr.predict(impl.kernel_matrix(x_test, x_train))).numpy()]
		tests += [mse(y_test2, regr.predict(impl.kernel_matrix(x_test2, x_train))).numpy()]
	idx = np.argmin(vals)
	val_impl = vals[idx]
	test_impl = tests[idx]
	err_impl = errs[idx]
	print('Best training loss, validation loss, test loss', err_impl, val_impl, test_impl)

	# # Train explicit model
	# nb_steps = 500
	# for i in range(nb_steps):
	# 	print(i, '/' + str(nb_steps))
	# 	l, val = trn.learning_step(x_train, y_train, x_test, y_test)
	# 	test = tf.keras.losses.MeanSquaredError()(trn.model(x_test2), y_test2).numpy()
	# 	trn.test_history += [test]
	# 	print('Training, validation, test: ', l, val, test)
	# 	if val.numpy()<10**(-5):
	# 		break
	# Train explicit model
	nb_steps = 500
	for i in range(nb_steps):
		print(i, '/' + str(nb_steps))
		# 确保使用 trn 对象自己的数据
		l, val = trn.learning_step(trn.y_train, trn.y_test)
		
		# 计算测试损失
		test = tf.keras.losses.MeanSquaredError()(
			trn.model(tf.convert_to_tensor(trn.x_test2_10)), 
			trn.y_test2
		).numpy()
		
		trn.test_history.append(test)
		print('Training, validation, test: ', l, val, test)
		if val < 10**(-5):
			break

	# 存储结果
	# 清理对象以便序列化
	def clean_for_pickling(obj):
		"""移除无法被 pickle 序列化的属性"""
		# 移除 TensorFlow 模型和优化器
		if hasattr(obj, 'model'):
			obj.model = None
		if hasattr(obj, 'optimizer_var'):
			obj.optimizer_var = None
		if hasattr(obj, 'optimizer_out'):
			obj.optimizer_out = None
		
		# 移除 TensorFlow 变量
		if hasattr(obj, 'variables'):
			obj.variables = None
		
		# 移除计算图相关引用
		if hasattr(obj, 'computation_layer'):
			obj.computation_layer = None
		if hasattr(obj, 'empty_circuit'):
			obj.empty_circuit = None
		
		# 对于 Implicit 对象
		if hasattr(obj, 'circuit'):
			obj.circuit = None
		if hasattr(obj, 'symbol_names'):
			obj.symbol_names = None

	# 清理所有需要序列化的对象
	clean_for_pickling(gen)
	clean_for_pickling(trn)
	clean_for_pickling(impl)
 
	pickle_path = f'./results/n{str(sys_args[1])}_L{str(sys_args[2])}_T{str(sys_args[3])}_{str(sys_args[4])}_fashion'
	if heisenberg:
		pickle_path += '_heisen'
	pickle_path += 'gauss.pckl'
	
	# pickle.dump([gen, trn, impl, d, g, err_0, val_0, test_0, errs, vals, tests], open(pickle_path, 'wb'))
	# 确保目录存在
	os.makedirs(os.path.dirname(pickle_path), exist_ok=True)

	# 保存数据
	with open(pickle_path, 'wb') as f:
		pickle.dump([gen, trn, impl, d, g, err_0, val_0, test_0, errs, vals, tests], f)
