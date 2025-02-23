# Copyright (c) 2022-2025, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# See LICENSE for license information.
"""RMSNorm API"""
import os
from typing import Union, Tuple

import paddle
from paddle.nn.initializer import Constant

from ..constants import TE_DType
from ..cpp_extensions import rmsnorm_fwd, rmsnorm_bwd
from ..distributed import mark_as_sequence_parallel_parameter

__all__ = ["RMSNorm"]


class _RMSNorm(paddle.autograd.PyLayer):
    """functional RMSNorm"""

    @staticmethod
    def forward(
        ctx,
        inp: paddle.Tensor,
        rmsnorm_weight: paddle.Tensor,
        eps: float,
        fwd_rmsnorm_sm_margin: int,
        bwd_rmsnorm_sm_margin: int,
        zero_centered_gamma: bool,
    ) -> paddle.Tensor:
        # Make sure input dimensions are compatible
        in_features = rmsnorm_weight.shape[0]
        assert inp.shape[-1] == in_features, "RMSNorm not possible"
        inputmat = inp.reshape((-1, in_features))

        rmsnorm_out, rsigma = rmsnorm_fwd(
            inputmat,
            rmsnorm_weight,
            eps,
            TE_DType[inp.dtype],
            fwd_rmsnorm_sm_margin,
            zero_centered_gamma,
        )

        ctx.save_for_backward(inputmat, rmsnorm_weight, rsigma)
        ctx.inp_shape = inp.shape
        ctx.bwd_rmsnorm_sm_margin = bwd_rmsnorm_sm_margin
        ctx.zero_centered_gamma = zero_centered_gamma
        ctx.requires_dx = not inp.stop_gradient
        ctx.requires_dw = not rmsnorm_weight.stop_gradient

        return rmsnorm_out.reshape(inp.shape)

    @staticmethod
    def backward(ctx, grad_output: paddle.Tensor) -> Tuple[Union[paddle.Tensor, None], ...]:
        inputmat, rmsnorm_weight, rsigma = ctx.saved_tensor()
        d_rmsnorm_out = grad_output.reshape(inputmat.shape)
        dxmat, dgamma = rmsnorm_bwd(
            d_rmsnorm_out,
            inputmat,
            rsigma,
            rmsnorm_weight,
            ctx.bwd_rmsnorm_sm_margin,
            ctx.zero_centered_gamma,
        )
        return (
            dxmat.reshape(ctx.inp_shape) if ctx.requires_dx else None,
            dgamma if ctx.requires_dw else None,
        )


class RMSNorm(paddle.nn.Layer):
    r"""
    Applies Root Mean Square Layer Normalization over a mini-batch of inputs as described in
    the paper `Root Mean Square Layer Normalization <https://arxiv.org/abs/1910.07467>`__

    .. math::
        y = \frac{x}{RMS_\varepsilon(x)} * \gamma

    where

    .. math::
        RMS_\varepsilon(x) = \sqrt{\frac{1}{n}\sum_{i=0}^nx_i^2 + \varepsilon}

    :math:`\gamma` is a learnable affine transform parameter of size :attr:`hidden_size`

    Parameters
    ----------
    hidden_size : int
                size of each input sample.
    eps : float, default = 1e-5
        a value added to the denominator of layer normalization for numerical stability.
    weight_attr: Union[paddle.ParamAttr, None], default = None
            optional `paddle.ParamAttr` for weight.
    zero_centered_gamma : bool, default = 'False'
                         if set to 'True', gamma parameter in RMSNorm is initialized to 0 and
                         the RMSNorm formula changes to

                         .. math::
                            y = \frac{x}{RMS(x) + \varepsilon} * (1 + \gamma)
    backend: {'transformer_engine', 'paddle'}, default = 'transformer_engine'
            backend to use for rmsnorm operation.

    Parallelism parameters
    ----------------------
    sequence_parallel : bool, default = `False`
                       if set to `True`, uses sequence parallelism.
    """

    def __init__(
        self,
        hidden_size: int,
        eps: float = 1e-5,
        weight_attr: Union[paddle.ParamAttr, None] = None,
        zero_centered_gamma: bool = False,
        sequence_parallel: bool = False,
        backend: str = "transformer_engine",
    ) -> None:
        super().__init__()

        self.eps = eps
        self.zero_centered_gamma = zero_centered_gamma
        self.sequence_parallel = sequence_parallel
        self.backend = backend
        self._dtype = self._helper.get_default_dtype()

        self._weight_attr = weight_attr
        if not self._weight_attr:
            self._weight_attr = paddle.ParamAttr(initializer=Constant(1.0))

        self.weight = self.create_parameter(
            shape=[hidden_size],
            attr=self._weight_attr,
            dtype=self._dtype,
            is_bias=False,
        )

        if self.sequence_parallel:
            mark_as_sequence_parallel_parameter(self.weight)

        # These many SMs are subtracted from the total SM count when calling forward
        # and backward RMSNorm C APIs. These envvars can be used to prevent the LN
        # kernels from using all SMs in the device. This is useful for cases such as
        # communication overlap with RMSNorm.
        self.fwd_rmsnorm_sm_margin = int(os.getenv("NVTE_FWD_LAYERNORM_SM_MARGIN", "0"))
        self.bwd_rmsnorm_sm_margin = int(os.getenv("NVTE_BWD_LAYERNORM_SM_MARGIN", "0"))

    def _te_forward(self, inp: paddle.Tensor) -> paddle.Tensor:
        return _RMSNorm.apply(
            inp,
            self.weight,
            self.eps,
            self.fwd_rmsnorm_sm_margin,
            self.bwd_rmsnorm_sm_margin,
            self.zero_centered_gamma,
        )

    def _pd_forward(
        self,
        inp: paddle.Tensor,
    ) -> paddle.Tensor:
        if self.zero_centered_gamma:
            raise NotImplementedError(
                "Paddle backend does not support RMSNorm with zero_centered_gamma."
            )
        norm = paddle.rsqrt(paddle.mean(inp**2, axis=-1, keepdim=True) + self.eps)
        y = inp * norm * self.weight
        return y

    def forward(self, *args, **kwargs):
        if self.backend == "transformer_engine":
            return self._te_forward(*args, **kwargs)
        if self.backend == "paddle":
            return self._pd_forward(*args, **kwargs)
        raise AttributeError(f"Backend {self.backend} not supported.")
