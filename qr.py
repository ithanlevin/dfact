"""
Jax implementation of householder QR exposing the low-level functionality
needed for the UTV decomposition.

Adam GM Lewis
adam.lws@gmail.com
alewis@perimeterinstitute.ca
"""
import numpy as np
import typing
from functools import partial

import jax
from jax.ops import index_update, index_add, index
import jax.numpy as jnp

import dfact.matutils as matutils
from dfact.matutils import dag

###############################################################################
# UTILITIES
###############################################################################
@jax.jit
def sign(num):
    """
    Sign function using the standard (?) convention sign(x) = x / |x| in
    the complex case. Returns 0 with the same type as x if x == 0.
    Note the numpy implementation uses the slightly different convention
    sign(x) = x / sqrt(x * x).
    """
    result = jax.lax.cond(num == 0,
                          num, lambda num: 0*num,  # 0, casted, if num is 0
                          num, lambda num: num/jnp.abs(num))  # else x/|x|
    return result


###############################################################################
# COMPUTATION OF HOUSEHOLDER VECTORS
###############################################################################
@jax.jit
def house(x_in):
    """
    Given a real or complex length-m vector x, finds the Householder vector
    v and its inverse normalization tau such that
                P = I - beta * v \otimes dag(v)
    is the symmetric (Hermitian) and orthogonal (unitary) Householder matrix
    representing reflections about x.

    Returns a list [v, beta], where v is a length-m vector whose first
    component is 1, and beta = 2/(dag(v) v).

    x will be treated as a flattened vector whatever its shape.

    Parameters
    ----------
    x_in:  array_like, shape(m,)
        The vector about which to compute the Householder reflector. Will
        be flattened (inside this fucntion only) to the prescribed shape.

    Output
    ------
    [v_out, beta]:
        v_out: array_like, shape(m,), the Householder vector including the 1.
        beta: float, the normalization 2/|v|
    """
    x_in = x_in.ravel()
    x_2_norm = jnp.linalg.norm(x_in[1:])
    # The next two lines are logically equivalent to
    # if x_2_norm == 0:
    #   v, beta = __house_zero_norm(x)
    # else:
    #   v, beta = __house_nonzero_norm( (x, x_2_norm) )
    switch = (x_2_norm == 0)
    v_out, beta = jax.lax.cond(switch,
                               x_in, __house_zero_norm,
                               (x_in, x_2_norm), __house_nonzero_norm)
    return [v_out, beta]


@jax.jit
def __house_zero_norm(x_in):
    """
    Handles house(x) in the case that norm(x[1:])==0.
    """
    beta = 2.
    v_out = x_in
    v_out = index_update(v_out, index[0], 1.)
    beta = jax.lax.cond(x_in[0] == 0,
                        x_in, lambda x: x[0]*0,
                        x_in, lambda x: x[0]*0 + 2
                       ).real
    return [v_out, beta]


@jax.jit
def __house_nonzero_norm(xtup):
    """
    Handles house(x) in the case that norm(x[1:])!=0.
    """
    x_in, x_2_norm = xtup
    x_in, x_2_norm = xtup
    x_norm = jnp.linalg.norm(jnp.array([x_in[0], x_2_norm]))
    rho = sign(x_in[0])*x_norm

    v_1p = x_in[0] + rho
    v_1pabs = jnp.abs(v_1p)
    v_1m = x_in[0] - rho
    v_1mabs = jnp.abs(v_1m)

    # Pick whichever of v[0] = x[0] +- sign(x[0])*||x||
    # has greater ||v[0]||, and thus leads to greater ||v||.
    # Golub and van Loan prescribes this "for stability".
    v_1, v_1abs = jax.lax.cond(v_1pabs >= v_1mabs,
                               (v_1p, v_1pabs), lambda x: x,
                               (v_1m, v_1mabs), lambda x: x)

    v_out = x_in
    v_out = index_update(v_out, index[1:], v_out[1:]/v_1)
    v_out = index_update(v_out, index[0], 1.)
    v_2_norm = x_2_norm / v_1abs
    v_norm_sqr = 1 + v_2_norm**2
    beta = (2 / v_norm_sqr).real
    return [v_out, beta]


###############################################################################
# MANIPULATION OF HOUSEHOLDER VECTORS
###############################################################################
@jax.jit
def form_dense_P(hlist):
    """
    Computes the dense Householder matrix P = I - beta * (v otimes dag(v))
    from the Householder reflector stored as hlist = (v, beta). This is
    useful for testing.
    """
    v, beta = hlist
    Id = jnp.eye(v.size, dtype=v.dtype)
    P = Id - beta * jnp.outer(v, dag(v))
    return P


@jax.jit
def house_leftmult(A, v, beta):
    """
    Given the m x n matrix A and the length-n vector v with normalization
    beta such that P = I - beta v otimes dag(v) is the Householder matrix that
    reflects about v, compute PA.

    Parameters
    ----------
    A:  array_like, shape(M, N)
        Matrix to be multiplied by H.

    v:  array_like, shape(N).
        Householder vector.

    beta: float
        Householder normalization.

    Returns
    -------
    C = PA
    """
    C = A - jnp.outer(beta*v, jnp.dot(dag(v), A))
    return C


@jax.jit
def house_rightmult(A, v, beta):
    """
    Given the m x n matrix A and the length-n vector v with normalization
    beta such that P = I - beta v otimes dag(v) is the Householder matrix that
    reflects about v, compute AP.

    Parameters
    ----------
    A:  array_like, shape(M, N)
        Matrix to be multiplied by H.

    v:  array_like, shape(N).
        Householder vector.

    beta: float
        Householder normalization.

    Returns
    -------
    C = AP
    """
    C = A - jnp.outer(A@v, beta*dag(v))
    return C


###############################################################################
# MANIPULATION OF FACTORED QR REPRESENTATION
###############################################################################
def factored_rightmult_dense(A, H, betas):
    """
    Computes C = A * Q, where Q is in the factored representation.
    With A = Hbetalist[0].shape[0], this computes Q, but less economically
    than 'factored_to_QR'.

    This is a dense implementation written to test 'factored_rightmult' below.
    Do not call it in production code.
    """
    C = A
    n = C.shape[1]
    for j, beta in enumerate(betas):
        vnz = jnp.array([1.]+list(H[j+1:, j]))
        nminus = n - vnz.size
        v = jnp.array([0.]*nminus + list(vnz))
        P = form_dense_P([v, beta])
        C = index_update(C, index[:, :], C@P)
    return C


@jax.jit
def factored_rightmult(A, H, betas):
    """
    Computes C = A * Q, where Q is in the factored representation.
    With A = Hbetalist[0].shape[0], this computes Q, but less economically
    than 'factored_to_QR'.
    """
    C = A
    for j, beta in enumerate(betas):
        v = jnp.array([1.]+list(H[j+1:, j]))
        C = index_update(C, index[:, j:], house_rightmult(C[:, j:], v, beta))
    return C


@jax.jit
def factored_to_QR(h, beta):
    """
    Computes dense matrices Q and R from the factored QR representation
    [h, tau] as computed by qr with mode == "factored".
    """
    m, n = h.shape
    R = jnp.triu(h)
    Q = jnp.eye(m, dtype=h.dtype)
    for j in range(n-1, -1, -1):
        v = jnp.concatenate((jnp.array([1.]), h[j+1:, j]))
        Q = index_update(Q, index[j:, j:],
                         house_leftmult(Q[j:, j:], v, beta[j]))
    out = [Q, R]
    return out

###############################################################################
# MANIPULATION OF WY QR REPRESENTATION
###############################################################################
@jax.jit
def times_householder_vector(A, H, j):
    """
    Computes A * v_j where v_j is the j'th Householder vector in H.

    Parameters
    ----------
    A: k x M matrix to multiply by v_j.
    H: M x k matrix of Householder reflectors.
    j: The column of H from which to extract v_j.

    Returns
    ------
    vout: length-M vector of output.
    """

    vin = jnp.array([1.]+list(H[j+1:, j]))
    vout = jnp.zeros(H.shape[0], dtype=H.dtype)
    vout = index_update(vout, index[j:], A[:, j:] @ vin)
    return vout


@jax.jit
def factored_to_WY(hbetalist):
    """
    Converts the 'factored' QR representation [H, beta] into the WY
    representation, Q = I - WY^H.

    Parameters
    ----------
    hbetalist = [H, beta] : list of array_like, shapes [M, N] and [N].
        'factored' QR rep of a matrix A (the output from
        house_QR(A, mode='factored')).

    Returns
    -------
    [W, YH]: list of ndarrays of shapes [M, N].
        The matrices W and Y generating Q along with R in the 'WY'
        representation.
    -W (M x N): The matrix W.
    -YH (M x N): -Y is the lower triangular matrix with the essential parts of
                  the Householder reflectors as its columns,
                  obtained by setting the main diagonal of H to 1 and zeroing
                  out everything above.
                 -YH, the h.c. of this matrix, is thus upper triangular
                  with the full Householder reflectors as its rows. This
                  function returns YH, which is what one needs to compute
                  C = Q @ B = (I - WY^H) @ B = B - W @ Y^H @ B.

                  Note: it is more efficient to store W and Y^H separately
                        than to precompute their product, since we will
                        typically have N << M when exploiting this
                        representation.
    """

    H, betas = hbetalist
    m, n = matutils.matshape(H)
    W = jnp.zeros(H.shape, H.dtype)
    vj = jnp.array([1.]+list(H[1:, 0]))
    W = index_update(W, index[:, 0], betas[0] * vj)

    Y = jnp.zeros(H.shape, H.dtype)
    Y = index_update(Y, index[:, 0], vj)
    for j in range(1, n):
        vj = index_update(vj, index[j+1:], H[j+1:, j])
        vj = index_update(vj, index[j], 1.)  # vj[j:] stores the current vector
        YHv = (dag(Y[j:, :j])) @ vj[j:]
        z = W[:, :j] @ YHv
        z = index_add(z, index[j:], -vj[j:])
        z = index_update(z, index[:], -betas[j]*z)

        W = index_update(W, index[:, j], z)
        Y = index_update(Y, index[j:, j], vj[j:])
    YH = dag(Y)
    return [W, YH]


@jax.jit
def B_times_Q_WY(B, W, YH):
    """
    Computes C(kxm) = B(kxm)@Q(mxm) with Q given as W and Y^H in
    Q = I(mxm) - W(mxr)Y^T(rxm).
    """
    C = B - (B@W)@YH
    return C


@jax.jit
def Qdag_WY_times_B(B, W, YH):
    """
    Computes C(mxk) = QH(mxm)@B(mxk) with Q given as W and Y^H in
    Q = I(mxm) - W(mxr)Y^T(rxm)
    """
    C = B - dag(YH)@(dag(W)@B)
    return C


@jax.jit
def WY_to_Q(W, YH):
    """
    Retrieves Q from its WY representation.
    """
    m = W.shape[0]
    Id = jnp.eye(m, dtype=W.dtype)
    return B_times_Q_WY(Id, W, YH)


###############################################################################
# QR DECOMPOSITION
###############################################################################
def house_qr(A, mode="reduced"):
    """
    Performs a QR decomposition of the m x n real or complex matrix A
    using the Householder algorithm.

    The string parameter 'mode' determines the representation of the output.
    In this way, one can retrieve various implicit representations of the
    factored matrices. This can be a significant optimization in the case
    of a highly rectangular A, which is the reason for this function's
    existence.

    Parameters
    ----------
    A : array_like, shape (M, N)
            Matrix to be factored.

        mode: {'reduced', 'complete', 'r', 'factored', 'WY'}, optional
            If K = min(M, N), then:
              - 'reduced': returns Q, R with dimensions (M, K), (K, N)
                (default)
              - 'complete': returns Q, R  with dimensions (M, M), (M, N)
              - 'r': returns r only with dimensions (K, N)
              - 'factored': returns H, beta with dimensions (N, M), (K), read
                 below for details.
              - 'WY' : returns W, Y with dimensions (M, K), read below for
                 details.

    With 'reduced', 'complete', or 'r', this function simply passes to
    jnp.linalg.qr, which depending on the currect status of Jax may lead to
    NotImplemented if A is complex.

    With 'factored' this function returns the same H, beta as generated by
    the Lapack function dgeqrf() (but in row-major form). Thus,
    H contains the upper triangular matrix R in its upper triangle, and
    the j'th Householder reflector forming Q in the j'th column of its
    lower triangle. beta[j] contains the normalization factor of the j'th
    reflector, called 'beta' in the function 'house' in this module.

    The matrix Q is then represented implicitly as
        Q = H(0) H(1) ... H(K), H(i) = I - tau[i] v dag(v)
    with v[:j] = 0; v[j]=1; v[j+1:]=A[j+1:, j].

    Application of Q (C -> dag{Q} C) can be made directly from this implicit
    representation using the function factored_multiply(C). When
    K << max(M, N), both the QR factorization and multiplication by Q
    using factored_multiply theoretically require far fewer operations than
    would an explicit representation of Q. However, these applications
    are mostly Level-2 BLAS operations.

    With 'WY' this function returns (M, K) matrices W and Y such that
        Q = I - W dag(Y).
    Y is lower-triangular matrix of Householder vectors, e.g. the lower
    triangle
    of the matrix H resulting from mode='factored'. W is then computed so
    that the above identity holds.

    Application of Q can be made directly from the WY representation
    using the function WY_multiply(C). The WY representation is
    a bit more expensive to compute than the factored one, though still less
    expensive than the full Q. Its advantage versus 'factored' is that
    WY_multiply calls depend mostly on Level-3 BLAS operations.


    Returns
    -------
    Q: ndarray of float or complex, optional
        The column-orthonormal orthogonal/unitary matrix Q.

    R: ndarray of float or complex, optional.
        The upper-triangular matrix.

    [H, beta]: list of ndarrays of float or complex, optional.
        The matrix H and scaling factors beta generating Q along with R in the
        'factored' representation.

    [W, Y, R] : list of ndarrays of float or complex, optional.
        The matrices W and Y generating Q along with R in the 'WY'
        representation.

    Raises
    ------
    LinAlgError
        If factoring fails.

    NotImplementedError
        In reduced, complete, or r mode with complex ijnp.t.
        In factored or WY mode in the case M < N.
    """
    if mode == "reduced" or mode == "complete" or mode == "r":
        return jnp.linalg.qr(A, mode=mode)
    else:
        m, n = A.shape
        if n > m:
            raise NotImplementedError("n > m QR not implemented in factored"
                                      + "or WY mode.")
        if mode == "factored":
            return __house_qr_factored(A)
        elif mode == "WY":
            hbetalist = __house_qr_factored(A)
            R = jnp.triu(hbetalist[0])
            WYlist = factored_to_WY(hbetalist)
            output = WYlist + [R]
            return output
        else:
            raise ValueError("Invalid mode: ", mode)


@jax.jit
def __house_qr_factored(A):
    """
    Computes the QR decomposition of A in the 'factored' representation.
    This is a workhorse function to be accessed externally by
    house_qr(A, mode="factored"), and is documented more extensively in
    that function's documentation.

    """
    H = A
    M, N = matutils.matshape(A)
    beta = list()
    for j in range(A.shape[1]):
        v, thisbeta = house(H[j:, j])
        beta.append(thisbeta)
        H = index_update(H, index[j:, j:], house_leftmult(H[j:, j:], v,
                                                          thisbeta))
        if j < M:
            H = index_update(H, index[j+1:, j], v[1:])
    beta = jnp.array(beta)
    output = [H, beta]
    return output


def __house_qr_factored_scan(A):
    """
    Computes the QR decomposition of A in the 'factored' representation.
    This is a workhorse function to be accessed externally by
    house_qr(A, mode="factored"), and is documented more extensively in
    that function's documentation.

    This implementation uses jax.lax.scan to reduce the amount of emitted
    XLA code.

    This should work for N > M!
    """
    H = A
    M, N = matutils.matshape(A)
    js_i = jnp.arange(0, M, 1)
    js_f = jnp.arange(M, N, 1)

    def house_qr_j_lt_m(H, j):
        m, n = H.shape
        Htri = jnp.tril(H)
        v, thisbeta = house_padded(Htri[:, j], j)
        #  Hjj = jax.lax.dynamic_slice(H, (j, j), (m-j, n-j))  # H[j:, j:]
        #  H_update = house_leftmult(Hjj, v, thisbeta)
        #  H = index_update(H, index[:, :],
        #                   jax.lax.dynamic_update_slice(H, H_update, [j, j]))
        #  H = index_update(H, index[:, :],
        #                   jax.lax.dynamic_update_slice(H, v[1:], [j+1, j]))
        return H, thisbeta

    def house_qr_j_gt_m(H, j):
        m, n = H.shape
        this_slice = jax.lax.dynamic_slice(H, (j, j), (m-j, 1))  # H[j:, j]
        v, thisbeta = house(this_slice)
        Hjj = jax.lax.dynamic_slice(H, (j, j), (m-j, n-j))  # H[j:, j:]
        H_update = house_leftmult(Hjj, v, thisbeta)
        H = index_update(H, index[:, :],
                         jax.lax.dynamic_update_slice(H, H_update, [j, j]))
        return H, thisbeta

    #  def house_qr_j_lt_m(H, j):
    #      m, n = H.shape
    #      H, thisbeta = house_qr_j_gt_m(H, j)
    #      v = jax.lax.dynamic_slice(H, (j+1, j), (m-j-1, 1))
    #      return H, thisbeta
    H, betas_i = jax.lax.scan(house_qr_j_lt_m, H, js_i)
    raise ValueError("Meep meep!")

    # H, betas_f = jax.lax.scan(house_qr_j_gt_m, H, js_f)

    betas = jnp.concatenate([betas_i, betas_f])
    output = [H, betas]
    return output


def recursiveQR(A, n_block=1):
    return __recursiveQR(A, n_block)


@partial(jax.jit, static_argnums=(1,))
def __recursiveQR(A, n_block):
    """
    Computes the QR decomposition of A using a blocked recursive strategy.

    n_block is a positive blocking parameter. A is recursively partitioned
    in half

     A (m x n) -> [A0 (m x n//2) | (A1 (m x n//2)]

    until n//2 is less than n_block, in which case the QR decomposition
    is computed using Jax QR. The matrix equation

    [A0 | A1] = [Q0 | Q1] [ R00 | R01 ]
                          [  0  | R11 ]

    is then used to reconstruct coarser from finer levels of recursion.

    Since this function ultimately calls Jax linalg, it will support
    e.g. complex numbers as soon as does the former.

    Parameters
    ----------
    A (m x n array-like):  The matrix to be decomposed.
    n_block:  an integer block-size.

    Returns
    -------
    [Q(m x n), R(n x n), A_update], the reduced QR decomposition of A.
        A would normally have been modified in place during the computation.
        Since this is not possible with Jax, a work array A_update is instead
        passed upwards through the recursion.
    """
    if n_block < 1:
        raise ValueError("n_block must be greater than 0; it was ", n_block)

    m, n = A.shape
    if n <= n_block:
        Q, R = jnp.linalg.qr(A, mode="reduced")
    else:
        n0 = n//2
        Q0, R00 = recursiveQR(A[:, :n0], n_block)
        R01 = dag(Q0) @ A[:, n0:]
        A = index_update(A, index[:, n0:], A[:, n0:] - Q0 @ R01)

        R0 = jnp.hstack([R00, R01])
        Q1, R11 = recursiveQR(A[:, n0:], n_block)
        Q = jnp.hstack([Q0, Q1])
        R10shape = (R11.shape[0], R00.shape[1])
        R10 = jnp.zeros(R10shape, R11.dtype)
        R1 = jnp.hstack([R10, R11])
        R = jnp.vstack([R0, R1])
    return [Q, R]


def recursiveQR_nojit(A, n_block: int):
    """
    Computes the QR decomposition of A using a blocked recursive strategy.

    n_block is a positive blocking parameter. A is recursively partitioned
    in half

     A (m x n) -> [A0 (m x n//2) | (A1 (m x n//2)]

    until n//2 is less than n_block, in which case the QR decomposition
    is computed using Jax QR. The matrix equation

    [A0 | A1] = [Q0 | Q1] [ R00 | R01 ]
                          [  0  | R11 ]

    is then used to reconstruct coarser from finer levels of recursion.

    Since this function ultimately calls Jax linalg, it will support
    e.g. complex numbers as soon as does the former.

    Parameters
    ----------
    A (m x n array-like):  The matrix to be decomposed.
    n_block:  an integer block-size.

    Returns
    -------
    [Q(m x n), R(n x n), A_update], the reduced QR decomposition of A.
        A would normally have been modified in place during the computation.
        Since this is not possible with Jax, a work array A_update is instead
        passed upwards through the recursion.
    """
    if n_block < 1:
        raise ValueError("n_block must be greater than 0; it was ", n_block)

    m, n = A.shape
    if n <= n_block:
        Q, R = jnp.linalg.qr(A, mode="reduced")
    else:
        n0 = n//2
        Q0, R00 = recursiveQR(A[:, :n0], n_block)
        R01 = dag(Q0) @ A[:, n0:]
        A = index_update(A, index[:, n0:], A[:, n0:] - Q0 @ R01)

        R0 = jnp.hstack([R00, R01])
        Q1, R11 = recursiveQR(A[:, n0:], n_block)
        Q = jnp.hstack([Q0, Q1])
        R10shape = (R11.shape[0], R00.shape[1])
        R10 = jnp.zeros(R10shape, R11.dtype)
        R1 = jnp.hstack([R10, R11])
        R = jnp.vstack([R0, R1])
    return [Q, R]


#  def block_power_svd(A, s=None, tol=1E-6):
#      if s is None:
#          s = A.shape[1]
#      return __block_power_svd(A, s, tol)

def block_power_svd(A, s=None, tol=1E-6, max_iter=10000):
    """
    Computes the SVD of A using the block power 'Chase' Method.

    The SVD is returned truncated to s; e.g. if A is m x n,

      U[m x s] @ Sigma [s x s] @ dag(V[n x s])

    This method returns the n x s matrix V, not dag(V). To reconstruct A
    you must first take the dag of V.

    Sigma is returned as a 1D array of length s, representing the diagonal
    elements (singular values).

    Perhaps surprisingly, this method seems to work even for s = n.

    Because numpy QR does not enforce that the diagonal entries of R be
    positive, the "singular values" returned here may be negative as well.
    Thus their absolute values should be considered when truncating.


    Parameters
    ----------
    A (m x n array_like): The matrix to be factored.
    s (int, default n): The SVD is returned truncated to this value.
    tol (float, default 1E-6): Iterate until this error threshold.
    max_iter (int, default 10000): Maximum number of iterations.
                                   A warning is printed if this is reached,
                                   but the function will return output as
                                   normal.


    Returns
    -------
    [U, Sigma, V] - The SVD.

    """
    m, n = A.shape
    if s < 0 or s >= n:
        raise ValueError(
          "s must be a smaller natural number than the columns of A; it was ",
          s)
    V = matutils.gaussian_random_fill(jnp.zeros((n, s), dtype=A.dtype))
    done = False
    n_iter = 0
    while not done:
        n_iter += 1
        U, Sigma, V, err = __block_svd_iteration(A, V, s)
        done = err <= tol or n_iter >= max_iter
        if n_iter >= max_iter:
            print("Warning: max_iter reached, err was: ", err)
    return [U, Sigma, V]


@partial(jax.jit, static_argnums=(2,))
def __block_svd_iteration(A, V, s):
    Ql, Rl = jnp.linalg.qr(A@V, mode="reduced")
    U = None
    U = Ql[:, :s]

    Qr, Rr = jnp.linalg.qr(dag(A)@U, mode="reduced")
    Sigma = jnp.diag(Rr[:s, :s])
    V = None
    V = Qr[:, :s]

    err_l = A@V
    err_r = U*Sigma
    err = jnp.linalg.norm(jnp.abs(err_l-err_r))
    return [U, Sigma, V, err]


















