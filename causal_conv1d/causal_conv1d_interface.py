# Copyright (c) 2023, Tri Dao.

import torch
import torch.nn.functional as F


import causal_conv1d_cuda


class CausalConv1dFn(torch.autograd.Function):

    @staticmethod
    def forward(ctx, x, weight, bias=None, hidden=None, seq_idx=None, activation=None):
        if activation not in [None, "silu", "swish"]:
            raise NotImplementedError("activation must be None, silu, or swish")
        if x.stride(2) != 1 and x.stride(1) != 1:
            x = x.contiguous()
        bias = bias.contiguous() if bias is not None else None
        seq_idx = seq_idx.contiguous() if seq_idx is not None else None
        ctx.save_for_backward(x, weight, bias, seq_idx)
        ctx.activation = activation in ["silu", "swish"]
        out = causal_conv1d_cuda.causal_conv1d_fwd(x, weight, bias, hidden, seq_idx, ctx.activation)
        return out

    @staticmethod
    def backward(ctx, dout):
        x, weight, bias, seq_idx = ctx.saved_tensors
        if dout.stride(2) != 1 and dout.stride(1) != 1:
            dout = dout.contiguous()
        # The kernel supports passing in a pre-allocated dx (e.g., in case we want to fuse the
        # backward of conv1d with the backward of chunk).
        # Here we just pass in None and dx will be allocated in the C++ code.
        dx, dweight, dbias = causal_conv1d_cuda.causal_conv1d_bwd(
            x, weight, bias, dout, seq_idx, None, ctx.activation
        )
        return dx, dweight, dbias if bias is not None else None, None, None


def causal_conv1d_fn(x, weight, bias=None, hidden=None, seq_idx=None, activation=None):
    """
    x: (batch, dim, seqlen)
    weight: (dim, width)
    bias: (dim,)
    seq_idx: (batch, seqlen)
    activation: either None or "silu" or "swish"

    out: (batch, dim, seqlen)
    """
    return CausalConv1dFn.apply(x, weight, bias, hidden, seq_idx, activation)


def causal_conv1d_ref(x, weight, bias=None, hidden=None, activation=None):
    """
    x: (batch, dim, seqlen)
    weight: (dim, width)
    bias: (dim,)
    hidden: (batch, dim, state_width) or None

    out: (batch, dim, seqlen)
    """
    if activation not in [None, "silu", "swish"]:
        raise NotImplementedError("activation must be None, silu, or swish")
    dtype_in = x.dtype
    x = x.to(weight.dtype)

    # Handle the initial state if provided
    if hidden is not None:
        # Ensure initial_state is of the same dtype as x
        hidden = hidden.to(x.dtype)
        # Concatenate initial_state with x along the sequence length dimension
        x = torch.cat([hidden, x], dim=-1)

    seqlen = x.shape[-1]
    dim, width = weight.shape
    out = F.conv1d(x, weight.unsqueeze(1), bias, padding=width - 1, groups=dim)
    out = out[..., :seqlen]

    # Adjust for the added initial state length if initial_state was provided
    if hidden is not None:
        out = out[..., hidden.shape[-1]:]

    return (out if activation is None else F.silu(out)).to(dtype_in)


def causal_conv1d_update(x, conv_state, weight, bias=None, activation=None):
    """
    x: (batch, dim)
    conv_state: (batch, dim, width)
    weight: (dim, width)
    bias: (dim,)

    out: (batch, dim)
    """
    if activation not in [None, "silu", "swish"]:
        raise NotImplementedError("activation must be None, silu, or swish")
    activation = activation in ["silu", "swish"]
    return causal_conv1d_cuda.causal_conv1d_update(x, conv_state, weight, bias, activation)


def causal_conv1d_update_ref(x, conv_state, weight, bias=None, activation=None):
    """
    x: (batch, dim)
    conv_state: (batch, dim, width)
    weight: (dim, width)
    bias: (dim,)

    out: (batch, dim)
    """
    if activation not in [None, "silu", "swish"]:
        raise NotImplementedError("activation must be None, silu, or swish")
    dtype_in = x.dtype
    batch, dim = x.shape
    width = weight.shape[1]
    assert conv_state.shape == (batch, dim, width)
    assert weight.shape == (dim, width)
    conv_state.copy_(torch.roll(conv_state, shifts=-1, dims=-1)) # Update state (B D W)
    conv_state[:, :, -1] = x
    out = torch.sum(conv_state * weight, dim=-1) # (B D)
    if bias is not None:
        out += bias
    return (out if activation is None else F.silu(out)).to(dtype=dtype_in)



