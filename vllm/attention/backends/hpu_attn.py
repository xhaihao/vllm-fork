###############################################################################
# Copyright (C) 2024 Habana Labs, Ltd. an Intel Company
###############################################################################

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Type

import torch
import vllm_hpu_extension.ops as ops
from vllm_hpu_extension.utils import Matmul, Softmax, VLLMKVCache

from vllm.attention.backends.abstract import (AttentionBackend, AttentionImpl,
                                              AttentionMetadata, AttentionType)
from vllm.attention.backends.utils import CommonAttentionState
from vllm.attention.ops.hpu_paged_attn import (HPUPagedAttention,
                                               HPUPagedAttentionMetadata)
from vllm.logger import init_logger
from vllm.utils import is_fake_hpu

logger = init_logger(__name__)


class HPUAttentionBackend(AttentionBackend):

    @staticmethod
    def get_name() -> str:
        return "hpu-attn"

    @staticmethod
    def get_impl_cls() -> Type["HPUAttentionImpl"]:
        return HPUAttentionImpl

    @staticmethod
    def get_metadata_cls() -> Type["AttentionMetadata"]:
        return HPUAttentionMetadata

    @staticmethod
    def get_state_cls() -> Type["CommonAttentionState"]:
        return CommonAttentionState

    @staticmethod
    def get_kv_cache_shape(
        num_blocks: int,
        block_size: int,
        num_kv_heads: int,
        head_size: int,
    ) -> Tuple[int, ...]:
        return HPUPagedAttention.get_kv_cache_shape(num_blocks, block_size,
                                                    num_kv_heads, head_size)

    @staticmethod
    def swap_blocks(
        src_kv_cache: torch.Tensor,
        dst_kv_cache: torch.Tensor,
        src_to_dsts: torch.Tensor,
    ) -> None:
        HPUPagedAttention.swap_blocks(src_kv_cache, dst_kv_cache, src_to_dsts)

    @staticmethod
    def copy_blocks(
        kv_caches: List[torch.Tensor],
        src_to_dsts: torch.Tensor,
    ) -> None:
        HPUPagedAttention.copy_blocks(kv_caches, src_to_dsts)


@dataclass
class HPUAttentionMetadata(HPUPagedAttentionMetadata, AttentionMetadata):
    """Metadata for HPUAttentionbackend."""
    # Currently, input sequences can only contain all prompts
    # or all decoding. True if all sequences are prompts.
    is_prompt: bool
    attn_bias: Optional[torch.Tensor]
    seq_lens_tensor: Optional[torch.Tensor]
    context_lens_tensor: Optional[torch.Tensor]


class HPUAttentionImpl(AttentionImpl, torch.nn.Module):
    """
    If the input tensors contain prompt tokens, the layout is as follows:
    |<--------------- num_prefill_tokens ----------------->|
    |<--prefill_0-->|<--prefill_1-->|...|<--prefill_N-1--->|

    Otherwise, the layout is as follows:
    |<----------------- num_decode_tokens ------------------>|
    |<--decode_0-->|..........|<--decode_M-1-->|<--padding-->|

    Generation tokens can contain padding when cuda-graph is used.
    Currently, prompt tokens don't contain any padding.

    The prompts might have different lengths, while the generation tokens
    always have length 1.
    """

    def __init__(
        self,
        num_heads: int,
        head_size: int,
        scale: float,
        num_kv_heads: int,
        alibi_slopes: Optional[List[float]],
        sliding_window: Optional[int],
        kv_cache_dtype: str,
        blocksparse_params: Optional[Dict[str, Any]] = None,
        max_seq_len: int = 4096,
    ) -> None:
        super(AttentionImpl, self).__init__()
        self.kv_cache_dtype = kv_cache_dtype
        self.num_heads = num_heads
        self.head_size = head_size
        self.scale = float(scale)
        self.matmul_qk = Matmul()
        self.softmax = Softmax()
        self.matmul_av = Matmul()
        self.batch2block_matmul = Matmul()
        self.block2batch_matmul = Matmul()
        self.k_cache = VLLMKVCache()
        self.v_cache = VLLMKVCache()
        self.num_kv_heads = num_heads if num_kv_heads is None else num_kv_heads
        self.sliding_window = sliding_window
        self.alibi_slopes = None
        self.prompt_position_bias = None
        self.max_seq_len = max_seq_len
        if alibi_slopes is not None:
            self.max_seq_len = int(os.getenv('VLLM_PROMPT_ALIBI_MAX_SEQ_LEN',
                                              self.max_seq_len))
            slope_tensor_dtype = {
                True: torch.float32,
                False: torch.bfloat16,
            }[os.getenv('VLLM_ALIBI_USE_FLOAT32_BIASES', '0').lower() in ['1', 'true']]
            alibi_slopes_tensor = torch.tensor(alibi_slopes,
                                               dtype=slope_tensor_dtype)
            self.alibi_slopes = alibi_slopes_tensor
            self.prompt_position_bias = _make_prompt_alibi_bias(
                self.alibi_slopes,
                self.num_kv_heads,
                self.alibi_slopes.dtype,
                self.max_seq_len,
            )
        assert self.num_heads % self.num_kv_heads == 0
        self.num_queries_per_kv = self.num_heads // self.num_kv_heads

        self.prefill_use_fusedsdpa = os.getenv('VLLM_PROMPT_USE_FUSEDSDPA',
                                               '1').lower() in ['1', 'true'] \
                                               and not is_fake_hpu()
        if self.prefill_use_fusedsdpa:
            assert alibi_slopes is None, \
                'Prefill with FusedSDPA not supported with alibi slopes!'

        self.use_contiguous_pa = os.environ.get('VLLM_CONTIGUOUS_PA',
                                                'true').lower() == 'true'
        if self.use_contiguous_pa:
            assert alibi_slopes is None, \
                'Contiguous PA not supported with alibi slopes!'

        if ops.ACTUAL_PA_SOFTMAX_IMPL != 'wsum_head_amax':
            assert alibi_slopes is None, \
                'Alibi slopes supports only "wsum_head_amax" softmax implementation!'

        suppored_head_sizes = HPUPagedAttention.get_supported_head_sizes()
        if head_size not in suppored_head_sizes:
            raise ValueError(
                f"Head size {head_size} is not supported by PagedAttention. "
                f"Supported head sizes are: {suppored_head_sizes}.")

    def forward(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        value: torch.Tensor,
        kv_cache: torch.Tensor,
        attn_metadata: HPUAttentionMetadata,
        k_scale: float = 1.0,
        v_scale: float = 1.0,
        attn_type: AttentionType = AttentionType.DECODER,
    ) -> torch.Tensor:
        """Forward pass with xFormers and PagedAttention.

        Args:
            query: shape = [num_tokens, num_heads * head_size]
            key: shape = [num_tokens, num_kv_heads * head_size]
            value: shape = [num_tokens, num_kv_heads * head_size]
            kv_cache = [2, num_blocks, block_size * num_kv_heads * head_size]
            attn_metadata: Metadata for attention.
        Returns:
            shape = [num_tokens, num_heads * head_size]
        """
        if attn_type != AttentionType.DECODER:
            raise NotImplementedError("Encoder self-attention and "
                                      "encoder/decoder cross-attention "
                                      "are not implemented for "
                                      "HPUAttentionImpl")
        batch_size, seq_len, hidden_size = query.shape
        _, seq_len_kv, _ = key.shape

        query = query.view(-1, self.num_heads, self.head_size)
        key = key.view(-1, self.num_kv_heads, self.head_size)
        value = value.view(-1, self.num_kv_heads, self.head_size)
        block_indices = attn_metadata.block_indices
        block_offsets = attn_metadata.block_offsets
        if attn_metadata.is_prompt:
            key = key.unflatten(0, (block_indices.size(0), -1))
            value = value.unflatten(0, (block_indices.size(0), -1))
        if kv_cache is not None:
            key_cache, value_cache = HPUPagedAttention.split_kv_cache(
                kv_cache, self.num_kv_heads, self.head_size)

            # Reshape the input keys and values and store them in the cache.
            # If kv_cache is not provided, the new key and value tensors are
            # not cached. This happens during the initial memory profiling run.
            key_cache = self.k_cache(key, key_cache, block_indices,
                                     block_offsets)
            value_cache = self.v_cache(value, value_cache, block_indices,
                                       block_offsets)

        if attn_metadata.is_prompt:
            # Prompt run.
            query_shape = (batch_size, seq_len, self.num_heads, self.head_size)
            kv_shape = (batch_size, seq_len_kv, self.num_kv_heads,
                        self.head_size)
            if attn_metadata is None or attn_metadata.block_list is None:
                if not self.prefill_use_fusedsdpa:
                    # TODO: move this outside of model
                    assert attn_metadata.attn_bias is not None, \
                            'attn_bias must be set before calling model.forward'
                    attn_bias = attn_metadata.attn_bias
                    position_bias = self.prompt_position_bias
                    if position_bias is not None:
                        position_bias = position_bias[:, :, -attn_bias.size(-2):, -attn_bias.size(-1):]
                else:
                    attn_bias = None
                    position_bias = None

                out = ops.prompt_attention(
                    query.view(query_shape),
                    key.view(kv_shape),
                    value.view(kv_shape),
                    attn_bias=attn_bias,
                    position_bias=position_bias,
                    p=0.0,
                    scale=self.scale,
                    matmul_qk_op=self.matmul_qk,
                    softmax_op=self.softmax,
                    matmul_av_op=self.matmul_av,
                    valid_seq_lengths=attn_metadata.seq_lens_tensor,
                )
            else:
                # TODO: enable FusedSDPA
                out = HPUPagedAttention.forward_prefix(
                    query=query.view(query_shape),
                    key=key.view(kv_shape),
                    value=value.view(kv_shape),
                    key_cache=key_cache,
                    value_cache=value_cache,
                    block_list=attn_metadata.block_list,
                    attn_bias=attn_metadata.attn_bias,
                    scale=self.scale,
                    matmul_qk_op=self.matmul_qk,
                    matmul_av_op=self.matmul_av,
                    softmax_op=self.softmax,
                    keys_fetch_func=self.k_cache.fetch_from_cache,
                    values_fetch_func=self.v_cache.fetch_from_cache)
            output = out.reshape(batch_size, seq_len, hidden_size)
        else:
            # Decoding run.
            position_bias = None
            attn_bias = attn_metadata.attn_bias
            alibi_blocks = attn_metadata.alibi_blocks
            if self.alibi_slopes is not None and alibi_blocks is not None:
                position_bias = _make_decode_alibi_bias(
                    alibi_blocks,
                    self.alibi_slopes,
                    self.num_kv_heads,
                    self.alibi_slopes.dtype,
                )

            output = HPUPagedAttention.forward_decode(
                query=query,
                key_cache=key_cache,
                value_cache=value_cache,
                block_list=attn_metadata.block_list,
                block_mapping=attn_metadata.block_mapping,
                block_bias=attn_metadata.attn_bias,
                block_scales=attn_metadata.block_scales,
                block_groups=attn_metadata.block_groups,
                scale=self.scale,
                position_bias=position_bias,
                matmul_qk_op=self.matmul_qk,
                matmul_av_op=self.matmul_av,
                batch2block_matmul_op=self.batch2block_matmul,
                block2batch_matmul_op=self.block2batch_matmul,
                keys_fetch_func=self.k_cache.fetch_from_cache,
                values_fetch_func=self.v_cache.fetch_from_cache,
            )

        # Reshape the output tensor.
        output = output.view(batch_size, seq_len, hidden_size)
        return output


def _make_prompt_alibi_bias(
    alibi_slopes: torch.Tensor,
    num_kv_heads: int,
    dtype: torch.dtype,
    seq_len: int,
) -> torch.Tensor:
    bias = torch.arange(seq_len, dtype=dtype)
    # NOTE(zhuohan): HF uses
    #     `bias = bias[None, :].repeat(seq_len, 1)`
    # here. We find that both biases give the same results, but
    # the bias below more accurately follows the original ALiBi
    # paper.
    # Calculate a matrix where each element represents ith element- jth
    # element.
    bias = bias[None, :] - bias[:, None]

    padded_len = (seq_len + 7) // 8 * 8
    num_heads = alibi_slopes.shape[0]
    per_head_bias = torch.empty(
        1,
        num_heads,
        seq_len,
        padded_len,
        device=alibi_slopes.device,
        dtype=dtype,
    )[:, :, :, :seq_len]
    # NOTE(Tanner):
    # .copy_ was not performing broadcasting of bias to all 32 heads in Eager mode.
    per_head_bias[:, :] = bias
    per_head_bias.mul_(alibi_slopes[:, None, None])
    if num_heads != num_kv_heads:
        per_head_bias = per_head_bias.unflatten(1, (num_kv_heads, num_heads // num_kv_heads))

    return per_head_bias


def _make_decode_alibi_bias(
    alibi_blocks: torch.Tensor,
    alibi_slopes: torch.Tensor,
    num_kv_heads: int,
    dtype: torch.dtype,
) -> torch.Tensor:
    num_heads = alibi_slopes.shape[0]
    per_head_bias = torch.empty(
        alibi_blocks.size(0),  # num blocks
        num_heads,
        alibi_blocks.size(-1),
        device=alibi_slopes.device,
        dtype=dtype,
    )
    # NOTE(Tanner):
    # .copy_ was not performing broadcasting of bias to all 32 heads in Eager mode.
    per_head_bias[:, :] = alibi_blocks.unsqueeze(-2)
    per_head_bias.mul_(alibi_slopes[None, :, None])
    if num_heads != num_kv_heads:
        per_head_bias = per_head_bias.unflatten(1, (num_kv_heads, num_heads // num_kv_heads))
    return per_head_bias
