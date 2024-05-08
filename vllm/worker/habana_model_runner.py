###############################################################################
# Copyright (C) 2024 Habana Labs, Ltd. an Intel Company
###############################################################################

import contextlib
import time
from enum import IntEnum
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

# for logging hpugraph capture
import tqdm
import pandas as pd
import tabulate

import os
import contextlib
import math
import itertools
import numpy as np
import torch
import torch.nn as nn
import habana_frameworks.torch as htorch
from habana_frameworks.torch.hpu.metrics import metric_localcontext

from vllm.attention import (AttentionMetadata, AttentionMetadataPerStage,
                            get_attn_backend)
from vllm.config import (DeviceConfig, LoadConfig, LoRAConfig, ModelConfig,
                         ParallelConfig, SchedulerConfig, VisionLanguageConfig)
from vllm.distributed import broadcast_tensor_dict
from vllm.distributed.device_communicators import custom_all_reduce
from vllm.logger import init_logger
from vllm.lora.layers import LoRAMapping
from vllm.lora.request import LoRARequest
from vllm.lora.worker_manager import LRUCacheWorkerLoRAManager
from vllm.model_executor import SamplingMetadata
from vllm.model_executor.model_loader import get_model
from vllm.sampling_params import SamplingParams, SamplingType
from vllm.sequence import SamplerOutput, SequenceData, SequenceGroupMetadata
from vllm.utils import (HabanaMemoryProfiler, async_tensor_h2d,
                        is_pin_memory_available, make_tensor_with_pad,
                        maybe_expand_dim, pad_to_max_length, format_bytes)

logger = init_logger(__name__)

_PAD_SLOT_ID = -1
LORA_WARMUP_RANK = 8
_BATCH_SIZE_ALIGNMENT = 16
# Capture graphs for token size 1, 2, 4, 8, 16, 32, 48, ..., 512.
# NOTE: _get_graph_batch_size needs to be updated if this list is changed.
_BATCH_SIZES_TO_CAPTURE = [1, 2, 4, 8] + [
    _BATCH_SIZE_ALIGNMENT * i for i in range(1, 33)
]

# Capture graphs for token size 1, 32, 64, 128, 256, 512, 768 ... 2048
_MAX_SEQ_LEN_ALIGNMENT = 256
_MAX_SEQ_LENS_TO_CAPTURE = [1, 32, 64, 128] + [
    _MAX_SEQ_LEN_ALIGNMENT * i for i in range(1, 9)
]


class PreparePromptMetadata(NamedTuple):
    input_tokens: List[int]
    input_positions: List[int]
    attn_metadata: Optional[AttentionMetadataPerStage]
    seq_lens: List[int]
    query_lens: List[int]
    lora_index_mapping: List[int]
    lora_prompt_mapping: List[int]
    lora_requests: Set[LoRARequest]
    multi_modal_input: Optional[torch.Tensor]
    slot_mapping: List[int]

    @classmethod
    def empty(cls):
        return PreparePromptMetadata(
            input_tokens=[],
            input_positions=[],
            attn_metadata=None,
            seq_lens=[],
            query_lens=[],
            lora_index_mapping=[],
            lora_prompt_mapping=[],
            lora_requests=set(),
            multi_modal_input=None,
            slot_mapping=[],
        )


class PrepareDecodeMetadata(NamedTuple):
    input_tokens: List[int]
    input_positions: List[int]
    attn_metadata: Optional[AttentionMetadata]
    lora_index_mapping: List[int]
    lora_prompt_mapping: List[int]
    lora_requests: Set[LoRARequest]
    slot_mapping: List[int]

    @classmethod
    def empty(cls):
        return PrepareDecodeMetadata(
            input_tokens=[],
            input_positions=[],
            attn_metadata=None,
            lora_index_mapping=[],
            lora_prompt_mapping=[],
            lora_requests=set(),
            slot_mapping=[],
        )


# How batches are constructed.
class BatchType(IntEnum):
    # Every batch is prefill.
    PREFILL = 0
    # Every batch is decode.
    DECODE = 1
    # Batch is a mixture of prefill and decode.
    MIXED = 2


class HabanaModelRunner:

    def __init__(
        self,
        model_config: ModelConfig,
        parallel_config: ParallelConfig,
        scheduler_config: SchedulerConfig,
        device_config: DeviceConfig,
        load_config: LoadConfig,
        lora_config: Optional[LoRAConfig],
        kv_cache_dtype: Optional[str] = "auto",
        is_driver_worker: bool = False,
        vision_language_config: Optional[VisionLanguageConfig] = None,
    ):
        self.model_config = model_config
        self.parallel_config = parallel_config
        self.scheduler_config = scheduler_config
        self.lora_config = lora_config
        self.load_config = load_config
        self.is_driver_worker = is_driver_worker

        # model_config can be None in tests/samplers/test_sampler.py.
        # FIXME(woosuk): This is a hack to make the tests work. Refactor this.
        self.sliding_window = (model_config.get_sliding_window()
                               if model_config is not None else None)
        self.device_config = (device_config
                              if device_config is not None else DeviceConfig())
        self.device = self.device_config.device

        # Set after load_model.
        self.lora_manager: LRUCacheWorkerLoRAManager = None

        self.graph_runner_class = HPUGraphRunner
        self.graph_runners: Dict[Tuple[int, int], self.graph_runner_class] = {}

        self.max_seq_len_to_capture = (self.model_config.max_seq_len_to_capture
                                       if self.model_config is not None else 0)

        self.pin_memory = is_pin_memory_available()
        self.kv_cache_dtype = kv_cache_dtype
        self.vision_language_config = vision_language_config

        self.attn_backend = get_attn_backend(
            self.model_config.dtype if model_config is not None else None)

        # Lazy initialization
        self.model: torch.nn.Module  # Set after load_model
        self.block_size: int  # Set after initial profiling.
        # When using CUDA graph, the input block tables must be padded to
        # max_seq_len_to_capture. However, creating the block table in
        # Python can be expensive. To optimize this, we cache the block table
        # in numpy and only copy the actual input content at every iteration.
        # The shape of the cached block table will be
        # (max batch size to capture, max context len to capture / block size).
        self.graph_block_tables: torch.Tensor  # Set after initial profiling.


    def load_model(self) -> None:
        with HabanaMemoryProfiler() as m:
            self.model = get_model(
                model_config=self.model_config,
                device_config=self.device_config,
                load_config=self.load_config,
                lora_config=self.lora_config,
                vision_language_config=self.vision_language_config,
                parallel_config=self.parallel_config,
                scheduler_config=self.scheduler_config,
            )

        self.model_memory_usage = m.consumed_memory
        logger.info(f"Loading model weights took "
                    f"{format_bytes(self.model_memory_usage)} ({format_bytes(HabanaMemoryProfiler.current_memory_usage())}/{format_bytes(HabanaMemoryProfiler.total_memory())} used)")

        if self.lora_config:
            assert hasattr(self.model, "supported_lora_modules"
                           ) and self.model.supported_lora_modules, (
                               "Model does not support LoRA")
            assert hasattr(
                self.model,
                "embedding_modules"), "Model does not have embedding_modules"
            assert hasattr(self.model, "embedding_padding_modules"
                           ), "Model does not have embedding_padding_modules"
            self.lora_manager = LRUCacheWorkerLoRAManager(
                self.scheduler_config.max_num_seqs,
                self.scheduler_config.max_num_batched_tokens, self.vocab_size,
                self.lora_config, self.device, self.model.embedding_modules,
                self.model.embedding_padding_modules)
            self.model = self.lora_manager.create_lora_manager(self.model)

    def set_block_size(self, block_size: int) -> None:
        self.block_size = block_size

        self.graph_block_tables = np.zeros(
            (max(_BATCH_SIZES_TO_CAPTURE), self.get_max_block_per_batch()),
            dtype=np.int32)

    def get_max_block_per_batch(self) -> int:
        block_size = self.block_size
        return (self.max_seq_len_to_capture + block_size - 1) // block_size

    def _prepare_prompt(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
    ) -> PreparePromptMetadata:
        input_tokens: List[List[int]] = []
        input_positions: List[List[int]] = []
        slot_mapping: List[List[int]] = []
        lora_index_mapping: List[List[int]] = []
        lora_prompt_mapping: List[List[int]] = []
        lora_requests: Set[LoRARequest] = set()

        seq_lens: List[int] = []
        context_lens: List[int] = []
        query_lens: List[int] = []
        prefix_block_tables: List[List[int]] = []
        multi_modal_input_list: List[torch.Tensor] = []

        if len(seq_group_metadata_list) == 0:
            return PreparePromptMetadata.empty()

        for seq_group_metadata in seq_group_metadata_list:
            assert seq_group_metadata.is_prompt
            seq_ids = list(seq_group_metadata.seq_data.keys())
            assert len(seq_ids) == 1
            seq_id = seq_ids[0]

            computed_block_nums = seq_group_metadata.computed_block_nums
            if (self.scheduler_config is not None
                    and self.scheduler_config.chunked_prefill_enabled
                    and not (computed_block_nums is None
                             or computed_block_nums == [])):
                raise RuntimeError(
                    "chunked prefill cannot be used with prefix caching "
                    "now.")

            token_chunk_size = seq_group_metadata.token_chunk_size
            seq_data = seq_group_metadata.seq_data[seq_id]
            context_len = seq_data.get_num_computed_tokens()
            # We should use get_len here because in case of preemption
            # it contains output tokens.
            seq_len = min(seq_data.get_len(), context_len + token_chunk_size)
            prompt_tokens = seq_data.get_token_ids()[context_len:seq_len]
            seq_lens.append(seq_len)

            # NOTE: This only works for oooooooxxx style attention.
            if computed_block_nums is not None and len(
                    computed_block_nums) > 0 and self.sliding_window is None:
                # Prefix is not supported with sliding_window
                context_len = len(computed_block_nums) * self.block_size
                prompt_tokens = prompt_tokens[context_len:]
                prefix_block_tables.append(computed_block_nums)
            elif self.scheduler_config.chunked_prefill_enabled:
                if seq_group_metadata.block_tables is not None:
                    # Prefill has chunked before.
                    block_table = seq_group_metadata.block_tables[seq_id]
                    prefix_block_tables.append(block_table)
                else:
                    # The first prefill.
                    prefix_block_tables.append([])
            else:
                prefix_block_tables.append([])
                # Right now, prefill start is always 0. However, this
                # assumption can be changed once chunked prefill is introduced.
                assert context_len == 0

            # actual prompt lens
            context_lens.append(context_len)
            if context_len != 0:
                import pdb; pdb.set_trace() # what happens if we hit that path??
            query_lens.append(seq_len - context_len)

            input_tokens.append(prompt_tokens)
            # NOTE(woosuk): Here we assume that the first token in the prompt
            # is always the first token in the sequence.
            input_positions.append(list(range(context_len, seq_len)))
            lora_id = seq_group_metadata.lora_int_id

            if lora_id > 0:
                lora_requests.add(seq_group_metadata.lora_request)

            lora_index_mapping += [lora_id] * (seq_len - context_len)
            lora_prompt_mapping.append(
                [lora_id] *
                (seq_len - context_len
                 if seq_group_metadata.sampling_params.prompt_logprobs else 1))

            if seq_group_metadata.multi_modal_data:
                multi_modal_input_list.append(
                    seq_group_metadata.multi_modal_data.data)

            if seq_group_metadata.block_tables is None:
                # During memory profiling, the block tables are not initialized
                # yet. In this case, we just use a dummy slot mapping.
                slot_mapping.append([_PAD_SLOT_ID] * seq_len)
                continue

            # Compute the slot mapping.
            slot_mapping.append([])
            block_table = seq_group_metadata.block_tables[seq_id]

            # Mask the [0, start_idx) tokens of the prompt with _PAD_SLOT_ID,
            # where start_idx is max(0, seq_len - sliding_window).
            # For example, if the prompt len is 10, sliding window is 8, and
            # block size is 4, the first two tokens are masked and the slot
            # mapping will be [-1, -1, 2, 3, 4, 5, 6, 7, 0, 1].
            start_idx = 0
            if self.sliding_window is not None:
                assert context_len == 0, (
                    "Prefix caching is currently not supported with "
                    "sliding window attention")
                start_idx = max(0, seq_len - self.sliding_window)
            for i in range(context_len, seq_len):
                if i < start_idx:
                    slot_mapping[-1].append(_PAD_SLOT_ID)
                    continue

                block_number = block_table[i // self.block_size]
                block_offset = i % self.block_size
                slot = block_number * self.block_size + block_offset
                slot_mapping[-1].append(slot)

        max_query_len = max(query_lens)
        max_seq_len = max(seq_lens)
        assert max_query_len > 0

        context_lens_tensor = torch.tensor(context_lens,
                                           dtype=torch.int,
                                           device=self.device)

        if multi_modal_input_list:
            assert self.vision_language_config, (
                "Multi-modal inputs are only supported by "
                "vision language models.")
            multi_modal_input = torch.cat(multi_modal_input_list,
                                          dim=0).to(self.device)
        else:
            multi_modal_input = None

        max_prompt_block_table_len = max(len(t) for t in prefix_block_tables)
        max_prompt_len = max(seq_lens)
        input_tokens = make_tensor_with_pad(input_tokens,
                                             max_prompt_len,
                                             pad=0,
                                             dtype=torch.long,
                                             device=self.device)
        
        input_positions = make_tensor_with_pad(input_positions,
                                                max_prompt_len,
                                                pad=0,
                                                dtype=torch.long,
                                                device=self.device)
        
        slot_mapping = make_tensor_with_pad(slot_mapping,
                                             max_prompt_len,
                                             pad=_PAD_SLOT_ID,
                                             dtype=torch.long,
                                             device=self.device)

        # Prepare prefix block tables
        max_prompt_block_table_len = max(len(t) for t in prefix_block_tables)
        block_tables = make_tensor_with_pad(
            prefix_block_tables,
            max_len=max_prompt_block_table_len,
            pad=0,
            dtype=torch.int,
            device=self.device,
        )

        # Query length can be shorter than key (i.e., prompt) when prefill
        # is chunked or prefix cached.
        query_lens_tensor = torch.tensor(query_lens,
                                         dtype=torch.long,
                                         device=self.device)
        subquery_start_loc = torch.zeros(query_lens_tensor.shape[0] + 1,
                                         dtype=torch.int32,
                                         device=self.device)

        seq_lens_tensor = torch.tensor(seq_lens,
                                       dtype=torch.long,
                                       device=self.device)
        seq_start_loc = torch.zeros(seq_lens_tensor.shape[0] + 1,
                                    dtype=torch.int32,
                                    device=self.device)

        torch.cumsum(query_lens_tensor,
                     dim=0,
                     dtype=subquery_start_loc.dtype,
                     out=subquery_start_loc[1:])

        torch.cumsum(seq_lens_tensor,
                     dim=0,
                     dtype=seq_start_loc.dtype,
                     out=seq_start_loc[1:])
        attn_metadata = self.attn_backend.make_metadata(
            is_prompt=True,
            seq_lens=seq_lens,
            seq_lens_tensor=seq_lens_tensor,
            max_query_len=max_query_len,
            max_seq_len=max_seq_len,
            subquery_start_loc=subquery_start_loc,
            seq_start_loc=seq_start_loc,
            context_lens_tensor=context_lens_tensor,
            block_tables=block_tables,
            use_cuda_graph=False,
        )
        return PreparePromptMetadata(
            input_tokens=input_tokens,
            input_positions=input_positions,
            attn_metadata=attn_metadata,
            seq_lens=seq_lens,
            query_lens=query_lens,
            lora_index_mapping=lora_index_mapping,
            lora_prompt_mapping=lora_prompt_mapping,
            lora_requests=lora_requests,
            multi_modal_input=multi_modal_input,
            slot_mapping=slot_mapping,
        )
    def _prepare_decode(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
    ) -> PrepareDecodeMetadata:
        input_tokens: List[List[int]] = []
        input_positions: List[List[int]] = []
        slot_mapping: List[List[int]] = []
        seq_lens: List[int] = []
        block_tables: List[List[int]] = []
        lora_index_mapping: List[int] = []
        lora_prompt_mapping: List[int] = []
        lora_requests: Set[LoRARequest] = set()

        if len(seq_group_metadata_list) == 0:
            return PrepareDecodeMetadata.empty()

        for seq_group_metadata in seq_group_metadata_list:
            assert not seq_group_metadata.is_prompt
            assert seq_group_metadata.token_chunk_size == 1

            seq_ids = list(seq_group_metadata.seq_data.keys())
            lora_id = seq_group_metadata.lora_int_id

            if lora_id > 0:
                lora_requests.add(seq_group_metadata.lora_request)

            for seq_id in seq_ids:
                seq_data = seq_group_metadata.seq_data[seq_id]
                generation_token = seq_data.get_last_token_id()
                input_tokens.append([generation_token])

                seq_len = seq_data.get_len()
                position = seq_len - 1
                input_positions.append([position])

                seq_len = seq_len if self.sliding_window is None else min(
                    seq_len, self.sliding_window)
                seq_lens.append(seq_len)

                block_table = seq_group_metadata.block_tables[seq_id]
                block_number = block_table[position // self.block_size]
                block_offset = position % self.block_size
                slot = block_number * self.block_size + block_offset
                slot_mapping.append([slot])
                lora_index_mapping.append(lora_id)
                lora_prompt_mapping.append(lora_id)

                if self.sliding_window is not None:
                    sliding_window_blocks = (self.sliding_window //
                                             self.block_size)
                    block_table = block_table[-sliding_window_blocks:]
                block_tables.append(block_table)

        # vLLM uses cuda graph only for decoding requests.
        # See `capture_model` API for more details.
        # For decoding requests, batch_size == input_tokens.
        batch_size = len(input_tokens)
        max_seq_len = max(seq_lens)
        use_captured_graph = (
            not self.model_config.enforce_eager
            and batch_size <= _BATCH_SIZES_TO_CAPTURE[-1]
            and max_seq_len <= self.max_seq_len_to_capture)
        if use_captured_graph:
            graph_batch_size = _get_graph_batch_size(batch_size)
            assert graph_batch_size >= batch_size
            for _ in range(graph_batch_size - batch_size):
                input_tokens.append([0])
                input_positions.append([0])
                slot_mapping.append([_PAD_SLOT_ID])
                seq_lens.append(1)
                block_tables.append([])
                lora_index_mapping.append(0)
            batch_size = graph_batch_size

        input_tokens = torch.tensor(input_tokens,
                                    dtype=torch.long,
                                    device=self.device)
        input_positions = torch.tensor(input_positions,
                                       dtype=torch.long,
                                       device=self.device)
        slot_mapping = torch.tensor(slot_mapping,
                                    dtype=torch.long,
                                    device=self.device)
        seq_lens_tensor = torch.tensor(seq_lens,
                                       dtype=torch.int,
                                       device=self.device)

        if use_captured_graph:
            # When using cuda-graph all these tensors should be
            # padded.
            assert seq_lens_tensor.shape[0] == len(input_tokens)
            assert seq_lens_tensor.shape[0] == len(input_positions)
            assert seq_lens_tensor.shape[0] == len(slot_mapping)

            # The shape of graph_block_tables is
            # [max batch size, max context len // block size].
            graph_max_seq_len  = _get_graph_max_seq_len(max_seq_len)
            assert graph_max_seq_len >= max_seq_len
            graph_block_count = math.ceil(graph_max_seq_len / self.block_size)
            input_block_tables = self.graph_block_tables[:batch_size, :graph_block_count]
            for i, block_table in enumerate(block_tables):
                if block_table:
                    input_block_tables[i, :len(block_table)] = block_table
            block_tables = torch.tensor(input_block_tables, device=self.device)
        else:
            max_block_table_len = max(
                len(block_table) for block_table in block_tables)
            block_tables = make_tensor_with_pad(
                block_tables,
                max_len=max_block_table_len,
                pad=0,
                dtype=torch.int,
                device=self.device,
            )
        attn_metadata = self.attn_backend.make_metadata(
            is_prompt=False,
            seq_lens=None,
            seq_lens_tensor=seq_lens_tensor,
            max_query_len=None,
            max_seq_len=max_seq_len,
            subquery_start_loc=None,
            seq_start_loc=None,
            context_lens_tensor=None,
            block_tables=block_tables,
            use_cuda_graph=use_captured_graph,
        )
        return PrepareDecodeMetadata(
            input_tokens=input_tokens,
            input_positions=input_positions,
            attn_metadata=attn_metadata,
            lora_index_mapping=lora_index_mapping,
            lora_prompt_mapping=lora_prompt_mapping,
            lora_requests=lora_requests,
            slot_mapping=slot_mapping,
        )


    def prepare_input_tensors(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
    ) -> Tuple[torch.Tensor, torch.Tensor, AttentionMetadata, SamplingMetadata,
               Set[LoRARequest], LoRAMapping, torch.Tensor]:
        if self.is_driver_worker:
            prefill_reqs = []
            decode_reqs = []
            for seq_group_meta in seq_group_metadata_list:
                if seq_group_meta.is_prompt:
                    prefill_reqs.append(seq_group_meta)
                else:
                    decode_reqs.append(seq_group_meta)

            # Prepare input tensors.
            (
                input_tokens,
                input_positions,
                prefill_attn_metadata,
                seq_lens,
                query_lens,
                lora_index_mapping,
                lora_prompt_mapping,
                lora_requests,
                multi_modal_input,
                slot_mapping,
            ) = self._prepare_prompt(prefill_reqs)
            (
                decode_input_tokens,
                decode_input_positions,
                decode_attn_metadata,
                decode_lora_index_mapping,
                decode_lora_prompt_mapping,
                decode_lora_requests,
                decode_slot_mapping,
            ) = self._prepare_decode(decode_reqs)
            sampling_metadata = SamplingMetadata.prepare(
                seq_group_metadata_list, seq_lens, query_lens, self.device,
                self.pin_memory)

            if not self.scheduler_config.chunked_prefill_enabled:
                assert (len(prefill_reqs) and len(decode_reqs)) == 0

            num_prefills = len(seq_lens)
            num_prefill_tokens = len(input_tokens)
            num_decode_tokens = len(decode_input_tokens)

            # NOTE(kzawora): Here we diverge from GPU code - we don't support mixed batches, so we either use decode or prefill inputs, without coalescing. 
            assert (num_prefills == 0 and num_decode_tokens > 0) or (num_prefills > 0 and num_decode_tokens == 0), "HPU does not support mixed batches!"
            if num_decode_tokens > 0:
                input_tokens = decode_input_tokens
                input_positions = decode_input_positions
                slot_mapping = decode_slot_mapping
                lora_index_mapping = decode_lora_index_mapping
                lora_prompt_mapping = decode_lora_prompt_mapping
                lora_requests = decode_lora_requests

            if self.lora_config:
                lora_mapping = LoRAMapping(
                    lora_index_mapping,
                    lora_prompt_mapping,
                )
            else:
                lora_mapping = None

            # Broadcast the metadata.
            # If batch contains both prefill and decode, it sends 2 broadcasts.
            # If it only contains 1 type, it triggers a single broadcast.
            if (prefill_attn_metadata is not None
                    and decode_attn_metadata is not None):
                batch_type = BatchType.MIXED
                raise NotImplementedError("Mixed batch is not supported on HPU")
            elif prefill_attn_metadata is not None:
                batch_type = BatchType.PREFILL
            else:
                batch_type = BatchType.DECODE

            metadata_dict = {
                "input_tokens": input_tokens,
                "input_positions": input_positions,
                "selected_token_indices":
                sampling_metadata.selected_token_indices,
                "lora_requests": lora_requests,
                "lora_mapping": lora_mapping,
                "multi_modal_input": multi_modal_input,
                "num_prefill_tokens": num_prefill_tokens,
                "num_decode_tokens": num_decode_tokens,
                "slot_mapping": slot_mapping,
                "num_prefills": num_prefills,
                "batch_type": batch_type,
            }
            if prefill_attn_metadata is not None:
                metadata_dict.update(prefill_attn_metadata.asdict_zerocopy())
            else:
                assert decode_attn_metadata is not None
                metadata_dict.update(decode_attn_metadata.asdict_zerocopy())
            broadcast_tensor_dict(metadata_dict, src=0)

            # Broadcast decode attn metadata for mixed batch type.
            # The additional broadcast costs 300us overhead on 4 A10 GPUs.
            # We can potentially reduce the overhead by coelescing tensors.
            if batch_type == BatchType.MIXED:
                assert decode_attn_metadata is not None
                metadata_dict = decode_attn_metadata.asdict_zerocopy()
                broadcast_tensor_dict(metadata_dict, src=0)
        else:
            metadata_dict = broadcast_tensor_dict(src=0)
            input_tokens = metadata_dict.pop("input_tokens")
            input_positions = metadata_dict.pop("input_positions")
            slot_mapping = metadata_dict.pop("slot_mapping")
            num_prefills = metadata_dict.pop("num_prefills")
            selected_token_indices = metadata_dict.pop(
                "selected_token_indices")
            lora_mapping = metadata_dict.pop("lora_mapping")
            lora_requests = metadata_dict.pop("lora_requests")
            multi_modal_input = metadata_dict.pop("multi_modal_input")
            num_prefill_tokens = metadata_dict.pop("num_prefill_tokens")
            num_decode_tokens = metadata_dict.pop("num_decode_tokens")
            batch_type = metadata_dict.pop("batch_type")

            # Create an attention metadata.
            prefill_attn_metadata = None
            decode_attn_metadata = None
            if batch_type == BatchType.PREFILL or batch_type == BatchType.MIXED:
                prefill_attn_metadata = self.attn_backend.make_metadata(
                    **metadata_dict)
            else:
                decode_attn_metadata = self.attn_backend.make_metadata(
                    **metadata_dict)
            sampling_metadata = SamplingMetadata(
                seq_groups=None,
                selected_token_indices=selected_token_indices,
                categorized_sample_indices=None,
                num_prompts=0,
            )

            # if it is a mixed batch, decode attn_metadata is broadcasted
            # separately.
            if batch_type == BatchType.MIXED:
                metadata_dict = broadcast_tensor_dict(src=0)
                decode_attn_metadata = self.attn_backend.make_metadata(
                    **metadata_dict)

        attn_metadata = AttentionMetadata(
            num_prefills=num_prefills,
            slot_mapping=slot_mapping,
            num_prefill_tokens=num_prefill_tokens,
            num_decode_tokens=num_decode_tokens,
            prefill_metadata=prefill_attn_metadata,
            decode_metadata=decode_attn_metadata,
            kv_cache_dtype=self.kv_cache_dtype,
        )

        return (input_tokens, input_positions, attn_metadata,
                sampling_metadata, lora_requests, lora_mapping,
                multi_modal_input)


    @torch.inference_mode()
    def execute_model(
        self,
        seq_group_metadata_list: Optional[List[SequenceGroupMetadata]],
        kv_caches: List[torch.Tensor],
    ) -> Optional[SamplerOutput]:
        (input_tokens, input_positions, attn_metadata, sampling_metadata,
         lora_requests, lora_mapping, multi_modal_input
         ) = self.prepare_input_tensors(seq_group_metadata_list)

        if self.lora_config:
            self.set_active_loras(lora_requests, lora_mapping)

        # Currently HPU graph is only supported by the decode phase.
        prefill_meta = attn_metadata.prefill_metadata
        decode_meta = attn_metadata.decode_metadata
        if prefill_meta is None and decode_meta.use_cuda_graph:
            graph_batch_size = input_tokens.shape[0]
            graph_block_count = decode_meta.block_tables.shape[1] 
            graph_runner_key = (graph_batch_size, graph_block_count)
            model_executable = self.graph_runners[graph_runner_key]
            logger.info(f"Executing {self.graph_runner_class.__name__} with batch {graph_batch_size}, block_count {graph_block_count} (context_len up to {graph_block_count*self.block_size}, currently {torch.max(decode_meta.seq_lens_tensor).item()})")
        else:
            model_executable = self.model
        execute_model_kwargs = {
            "input_ids": input_tokens,
            "positions": input_positions,
            "kv_caches": kv_caches,
            "attn_metadata": attn_metadata,
        }
        if self.vision_language_config:
            execute_model_kwargs.update({"image_input": multi_modal_input})
        hidden_states = model_executable(**execute_model_kwargs)
        hidden_states = hidden_states.view(-1, hidden_states.shape[-1])

        # Compute the logits.
        logits = self.model.compute_logits(hidden_states, sampling_metadata)

        # Only perform sampling in the driver worker.
        if not self.is_driver_worker:
            return None

        # Sample the next token.
        output = self.model.sample(
            logits=logits,
            sampling_metadata=sampling_metadata,
        )

        return output

    @torch.inference_mode()
    def profile_run(self) -> None:
        # Enable top-k sampling to reflect the accurate memory usage.
        sampling_params = SamplingParams(top_p=0.99, top_k=self.vocab_size - 1)
        max_num_batched_tokens = self.scheduler_config.max_num_batched_tokens
        max_num_seqs = self.scheduler_config.max_num_seqs

        # This represents the maximum number of different requests
        # that will have unique loras, an therefore the max amount of memory
        # consumption create dummy lora request copies from the lora request
        # passed in, which contains a lora from the lora warmup path.
        dummy_lora_requests = []
        dummy_lora_requests_per_seq = []
        if self.lora_config:
            for idx in range(self.lora_config.max_loras):
                lora_id = idx + 1
                dummy_lora_request = LoRARequest(
                    lora_name=f"warmup_{lora_id}",
                    lora_int_id=lora_id,
                    lora_local_path="/not/a/real/path",
                )
                self.lora_manager.add_dummy_lora(dummy_lora_request,
                                                 rank=LORA_WARMUP_RANK)
                dummy_lora_requests.append(dummy_lora_request)
            dummy_lora_requests_per_seq = [
                dummy_lora_requests[idx % len(dummy_lora_requests)]
                for idx in range(max_num_seqs)
            ]

        # Profile memory usage with max_num_sequences sequences and the total
        # number of tokens equal to max_num_batched_tokens.
        seqs: List[SequenceGroupMetadata] = []
        # Additional GPU memory may be needed for vision encoding, which needs
        # to be accounted for when calculating the GPU blocks for
        # vLLM blocker manager.
        # To exercise the worst scenario for GPU memory consumption,
        # the number of seqs (batch_size) is chosen to maximize the number
        # of images processed.
        if self.vision_language_config:
            max_num_seqs = min(
                max_num_seqs,
                int(max_num_batched_tokens /
                    self.vision_language_config.image_feature_size))
        for group_id in range(max_num_seqs):
            seq_len = (max_num_batched_tokens // max_num_seqs +
                       (group_id < max_num_batched_tokens % max_num_seqs))
            seq_data = SequenceData([0] * seq_len)
            seq = SequenceGroupMetadata(
                request_id=str(group_id),
                is_prompt=True,
                seq_data={group_id: seq_data},
                sampling_params=sampling_params,
                block_tables=None,
                lora_request=dummy_lora_requests_per_seq[group_id]
                if dummy_lora_requests_per_seq else None,
            )
            seqs.append(seq)

        # Run the model with the dummy inputs.
        num_layers = self.model_config.get_num_layers(self.parallel_config)
        kv_caches = [None] * num_layers
        self.execute_model(seqs, kv_caches)
        torch.hpu.synchronize()
        return

    def remove_all_loras(self):
        if not self.lora_manager:
            raise RuntimeError("LoRA is not enabled.")
        self.lora_manager.remove_all_loras()

    def set_active_loras(self, lora_requests: Set[LoRARequest],
                         lora_mapping: LoRAMapping) -> None:
        if not self.lora_manager:
            raise RuntimeError("LoRA is not enabled.")
        self.lora_manager.set_active_loras(lora_requests, lora_mapping)

    def add_lora(self, lora_request: LoRARequest) -> bool:
        if not self.lora_manager:
            raise RuntimeError("LoRA is not enabled.")
        return self.lora_manager.add_lora(lora_request)

    def remove_lora(self, lora_id: int) -> bool:
        if not self.lora_manager:
            raise RuntimeError("LoRA is not enabled.")
        return self.lora_manager.remove_lora(lora_id)

    def list_loras(self) -> Set[int]:
        if not self.lora_manager:
            raise RuntimeError("LoRA is not enabled.")
        return self.lora_manager.list_loras()

    @torch.inference_mode()
    def capture_model(self, kv_caches: List[torch.Tensor]) -> None:
        """Cuda graph capture a model.

        Note that CUDA graph's performance gain is negligible if number
        of batched tokens are larger than 200. And since CUDA graph
        requires fixed sized tensors, supporting large/variable batch
        size requires high GPU memory overhead. Thus, vLLM only captures
        decoding requests. Mixed batch (chunked prefill + decoding) or
        prefill requests are not captured.

        Since it is used for decoding-only, it assumes there's only 1 token
        per sequence in the batch.
        """
        # NOTE(woosuk): This is a hack to ensure that the NCCL backend is never
        # deleted before the CUDA graphs.

        assert not self.model_config.enforce_eager
        logger.info("Capturing the model for HPUGraphs. This may lead to "
                    "unexpected consequences if the model is not static. To "
                    "run the model in eager mode, set 'enforce_eager=True' or "
                    "use '--enforce-eager' in the CLI.")
        logger.info("HPUGraphs can take additional ~10 GiB memory per HPU. "
                    "If you are running out of memory, consider decreasing "
                    "`gpu_memory_utilization` or enforcing eager mode. "
                    "You can also reduce the `max_num_seqs` as needed "
                    "to decrease memory usage.")
        start_time = time.perf_counter()

        # Prepare dummy inputs. These will be reused for all batch sizes.
        max_batch_size = max(_BATCH_SIZES_TO_CAPTURE)
        input_tokens = torch.zeros(max_batch_size, 1, dtype=torch.long).to('hpu')
        input_positions = torch.zeros(max_batch_size, 1, dtype=torch.long).to('hpu')
        slot_mapping = torch.zeros(max_batch_size, 1, dtype=torch.long).to('hpu') # TODO(kzawora): when using torch.empty, following occurs: RuntimeError: Error when trying to cast Long to Int, Input values range [0, 139632108750000] exceeds Int range [-2147483648, 2147483647]
        slot_mapping.fill_(_PAD_SLOT_ID)
        context_lens = torch.ones(max_batch_size, dtype=torch.int32).to('hpu')
        block_tables = torch.from_numpy(self.graph_block_tables).to('hpu')

        graph_batch_size = _get_graph_batch_size(
            self.scheduler_config.max_num_seqs)
        batch_size_capture_list = [
            bs for bs in _BATCH_SIZES_TO_CAPTURE if bs <= graph_batch_size
        ]

        # NOTE(woosuk): There are 3 backends for all-reduce: custom all-reduce
        # kernel, CuPy NCCL, and PyTorch NCCL. When using CUDA graph, we use
        # either custom all-reduce kernel or CuPy NCCL. When not using CUDA
        # graph, we use either custom all-reduce kernel or PyTorch NCCL.
        # We always prioritize using custom all-reduce kernel but fall back
        # to PyTorch or CuPy NCCL if it is disabled or not supported.
        with custom_all_reduce.capture():
            # NOTE: Capturing the largest batch size first may help reduce the
            # memory usage of CUDA graph.
            valid_combinations = []
            total_combinations = len(_BATCH_SIZES_TO_CAPTURE)*len(_MAX_SEQ_LENS_TO_CAPTURE)
            import pandas as pd
            df = pd.DataFrame(index=_BATCH_SIZES_TO_CAPTURE, columns=_MAX_SEQ_LENS_TO_CAPTURE)
            for idx, (batch_size, max_seq_len) in enumerate(itertools.product(reversed(_BATCH_SIZES_TO_CAPTURE), reversed(_MAX_SEQ_LENS_TO_CAPTURE))): 
                block_count = math.ceil(max_seq_len / self.block_size)
                # Skip capture of "out-of-bound" batch sizes and context lengths
                if batch_size > self.scheduler_config.max_num_seqs:
                    logger.debug(f"[{idx}/{total_combinations}] Skipping capture for batch {batch_size}, max_seq_len {max_seq_len}, block_count {block_count}. Reason: Batch out of bound.")
                    df[max_seq_len][batch_size] = 'batch OoB'
                    continue 
                if max_seq_len > self.max_seq_len_to_capture:
                    logger.debug(f"[{idx}/{total_combinations}] Skipping capture for batch {batch_size}, max_seq_len {max_seq_len}, block_count {block_count}. Reason: Nax context length out of bound.")
                    df[max_seq_len][batch_size] = 'ctx OoB'
                    continue
                block_count = math.ceil(max_seq_len / self.block_size)
                captured_block_counts = [math.ceil(cl / self.block_size) for (n, cl) in valid_combinations if n == batch_size]
                if block_count in captured_block_counts:
                    logger.debug(f"[{idx}/{total_combinations}] Skipping capture for batch {batch_size}, max_seq_len {max_seq_len}, block_count {block_count}. Reason: Block size already captured.")
                    df[max_seq_len][batch_size] = 'redundant'
                    continue
                logger.debug(f"[{idx}/{total_combinations}] Will capture for batch {batch_size}, max_seq_len {max_seq_len}, block_count {block_count}. Constraints met.")
                df[max_seq_len][batch_size] = 'VALID'
                valid_combinations.append((batch_size, max_seq_len))

            total_valid_hpugraphs = len(valid_combinations)
            logger.info(f"Starting capture {total_valid_hpugraphs} valid HPUGraphs. Skipping capture of {total_combinations-total_valid_hpugraphs}/{total_combinations} graphs due to batch/context constraints.")
            logger.debug(f"Capture summary (row: batch_size; col: max_seq_len):")
            logger.debug(tabulate.tabulate(df, tablefmt='mixed_outline', headers='keys', showindex="always"))

            graph_runner_name = self.graph_runner_class.__name__
            graph_mem_usage_df = pd.DataFrame(index=list(reversed(sorted({b for b,c in valid_combinations}))), columns=list(reversed(sorted({c for b,c in valid_combinations}))))
            pbar = tqdm.tqdm(valid_combinations)
            start_mem = HabanaMemoryProfiler.current_memory_usage()
            log_graph_compilation_all = os.environ.get('VLLM_HPU_LOG_STEP_GRAPH_COMPILATION_ALL', '0') != '0'
            log_graph_compilation = os.environ.get('VLLM_HPU_LOG_STEP_GRAPH_COMPILATION', '0') != '0' or log_graph_compilation_all
        
            for idx, (batch_size, max_seq_len) in enumerate(pbar): 
                block_count = math.ceil(max_seq_len / self.block_size)
                # Create dummy attn_metadata.
                decode_metadata = self.attn_backend.make_metadata(
                    is_prompt=False,
                    seq_lens=None,
                    seq_lens_tensor=context_lens[:batch_size],
                    max_query_len=None,
                    max_seq_len=block_count*self.block_size,
                    subquery_start_loc=None,
                    seq_start_loc=None,
                    context_lens_tensor=None, # NOTE(kzawora): this seems sus, shoudn't we have seq_lens tensor here?
                    block_tables=block_tables[:batch_size, :block_count],
                    use_cuda_graph=True,
                )
                attn_metadata = AttentionMetadata(
                    num_prefills=0,
                    num_prefill_tokens=0,
                    num_decode_tokens=batch_size,
                    slot_mapping=slot_mapping[:batch_size],
                    prefill_metadata=None,
                    decode_metadata=decode_metadata,
                    kv_cache_dtype=self.kv_cache_dtype,
                )

                if self.lora_config:
                    lora_mapping = LoRAMapping(
                        [0] * batch_size,
                        [0] * batch_size,
                    )
                    self.set_active_loras(set(), lora_mapping)
                graph_runner = self.graph_runner_class(self.model)
                local_start_mem = HabanaMemoryProfiler.current_memory_usage()
                capture_start = time.time()
                desc = f'Capturing {graph_runner_name} for batch {batch_size}, max_seq_len {max_seq_len}, block_count {block_count}, allocated {format_bytes(local_start_mem - start_mem)} device memory in total ({format_bytes(HabanaMemoryProfiler.current_memory_usage())}/{format_bytes(HabanaMemoryProfiler.total_memory())} used)'
                pbar.set_description(desc)
                logger.debug(f"[{idx}/{total_valid_hpugraphs}] {desc}...")
                profiling_ctx = contextlib.nullcontext() if not (log_graph_compilation_all or log_graph_compilation) else metric_localcontext("graph_compilation")
                with profiling_ctx as gc_local_metric:
                    graph_runner.capture(
                        input_tokens[:batch_size],
                        input_positions[:batch_size],
                        kv_caches,
                        attn_metadata,
                    )
                if (log_graph_compilation and gc_local_metric.stats()[0][1] > 0) or log_graph_compilation_all:
                    logger.info(f"VLLM_HPU_STEP_GRAPH_COMPILATION: {gc_local_metric.stats()}, {graph_runner_name}; batch {batch_size}, max_seq_len {max_seq_len}, block_count {block_count}")
                self.graph_runners[(batch_size, block_count)] = graph_runner
                capture_end = time.time()
                local_end_mem = HabanaMemoryProfiler.current_memory_usage()
                mem_usage_str = format_bytes(local_end_mem - local_start_mem)
                graph_mem_usage_df[max_seq_len][batch_size] = mem_usage_str
                logger.debug(f"[{idx}/{total_valid_hpugraphs}] {desc}... done in {capture_end-capture_start:.2f} seconds! Took {mem_usage_str} of device memory ({format_bytes(HabanaMemoryProfiler.current_memory_usage())}/{format_bytes(HabanaMemoryProfiler.total_memory())} used)")

        end_time = time.perf_counter()
        elapsed_time = end_time - start_time
        # This usually takes < 10 seconds.
        end_mem = HabanaMemoryProfiler.current_memory_usage()
        logger.info(f"Graph capturing finished in {elapsed_time:.0f} secs, allocated {format_bytes(end_mem - start_mem)} of device memory ({format_bytes(HabanaMemoryProfiler.current_memory_usage())}/{format_bytes(HabanaMemoryProfiler.total_memory())} used)")
        logger.info(f"Graph memory allocation summary (row: batch_size; col: max_seq_len):")
        logger.info(tabulate.tabulate(graph_mem_usage_df, tablefmt='mixed_outline', headers='keys', showindex="always"))

    def __del__(self) -> None:
        # Delete the CUDA graphs before deleting the CuPy NCCL communicator.
        # NOTE(woosuk): This is necessary because otherwise deadlocks can
        # happen.
        # FIXME(woosuk): This is a bit hacky. Find a more robust solution.
        self.graph_runners.clear()

    @property
    def vocab_size(self) -> int:
        return self.model_config.get_vocab_size()


class FakeHPUGraphRunner:

    def __init__(self, model: nn.Module):
        self.model = model

    def capture(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[torch.Tensor],
        attn_metadata: AttentionMetadata,
    ) -> None:
        return
    
    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[torch.Tensor],
        attn_metadata: AttentionMetadata,
    ) -> torch.Tensor:
        return self.model(
            input_ids,
            positions,
            kv_caches,
            attn_metadata,
        )

    def __call__(self, *args, **kwargs):
        return self.forward(*args, **kwargs)

class FakeHPUGraphRunnerWithWarmup:

    def __init__(self, model: nn.Module):
        self.model = model

    def capture(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[torch.Tensor],
        attn_metadata: AttentionMetadata,
    ) -> None:
        htorch.core.mark_step()
        out = self.model(
            input_ids,
            positions,
            kv_caches,
            attn_metadata,
        )
        htorch.core.mark_step()
        htorch.hpu.synchronize()
        return
    
    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[torch.Tensor],
        attn_metadata: AttentionMetadata,
    ) -> torch.Tensor:
        htorch.core.mark_step()
        out = self.model(
            input_ids,
            positions,
            kv_caches,
            attn_metadata,
        )
        htorch.core.mark_step()
        return out

    def __call__(self, *args, **kwargs):
        return self.forward(*args, **kwargs)
class HPUGraphRunner:

    def __init__(self, model: nn.Module):
        self.model = model
        self.graph = None
        self.input_buffers: Dict[str, torch.Tensor] = {}
        self.output_buffers: Dict[str, torch.Tensor] = {}

    def capture(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[torch.Tensor],
        attn_metadata: AttentionMetadata,
    ) -> None:
        assert self.graph is None
        # Run the model once without capturing the graph.
        # This is to make sure that the captured graph does not include the
        # kernel launches for initial benchmarking (e.g., Triton autotune).
        self.model(
            input_ids,
            positions,
            kv_caches,
            attn_metadata,
        )
        htorch.hpu.synchronize()

        # Capture the graph.
        # NOTE(woosuk): Python 3.8 does not support multi-line with statements.
        # https://stackoverflow.com/questions/31039022/python-multi-line-with-statement
        self.graph = htorch.hpu.HPUGraph()
        with htorch.hpu.graph(self.graph):  # noqa: SIM117
            hidden_states = self.model(
                input_ids,
                positions,
                kv_caches,
                attn_metadata,
            )
        torch.hpu.synchronize()

        # Save the input and output buffers.
        self.input_buffers = {
            "input_ids": input_ids,
            "positions": positions,
            "kv_caches": kv_caches,
            "slot_mapping": attn_metadata.slot_mapping,
            "seq_lens_tensor": attn_metadata.decode_metadata.seq_lens_tensor,
            "block_tables": attn_metadata.decode_metadata.block_tables,
        }
        self.output_buffers = {"hidden_states": hidden_states}
        return

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[torch.Tensor],
        attn_metadata: AttentionMetadata,
        **kwargs,
    ) -> torch.Tensor:
        # KV caches are fixed tensors, so we don't need to copy them.
        del kv_caches

        # Copy the input tensors to the input buffers.
        self.input_buffers["input_ids"].copy_(input_ids, non_blocking=True)
        self.input_buffers["positions"].copy_(positions, non_blocking=True)
        self.input_buffers["slot_mapping"].copy_(attn_metadata.slot_mapping,
                                                 non_blocking=True)
        self.input_buffers["seq_lens_tensor"].copy_(
            attn_metadata.decode_metadata.seq_lens_tensor, non_blocking=True)
        self.input_buffers["block_tables"].copy_(
            attn_metadata.decode_metadata.block_tables, non_blocking=True)
        # Run the graph.
        self.graph.replay()

        # Return the output tensor.
        return self.output_buffers["hidden_states"]

    def __call__(self, *args, **kwargs):
        return self.forward(*args, **kwargs)

class ExperimentalHPUGraphRunner:
    def __init__(self, model: nn.Module):
        self.model = model

    def capture(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[torch.Tensor],
        attn_metadata: AttentionMetadata,
    ) -> None:
        class ModelWrapper(torch.nn.Module):
            def __init__(self, model):
                super().__init__()
                self.model = model
                self.attn_backend = get_attn_backend(torch.bfloat16)
            def forward(self, input_ids, positions, kv_caches, slot_mapping, context_lens, block_tables):
                wrapper_attn_metadata = self.attn_backend.make_metadata(
                    is_prompt=attn_metadata.is_prompt,
                    seq_lens=None,
                    seq_lens_tensor=None, 
                    num_prefill_tokens=0,
                    num_generation_tokens=attn_metadata.num_generation_tokens,
                    max_subquery_len=None,
                    max_seq_len=attn_metadata.max_seq_len,
                    max_prompt_len=None,
                    subquery_start_loc=None,
                    seq_start_loc=None,
                    context_lens=context_lens,
                    block_tables=block_tables,
                    use_cuda_graph=True,
                    kv_cache_dtype=attn_metadata.kv_cache_dtype,
                )
                return self.model(
                    input_ids,
                    positions,
                    kv_caches,
                    wrapper_attn_metadata
                )
        self.graph_model = htorch.hpu.wrap_in_hpu_graph(ModelWrapper(self.model))
        out = self.graph_model(
            input_ids,
            positions,
            kv_caches,
            attn_metadata.slot_mapping,
            attn_metadata.context_lens, 
            attn_metadata.block_tables,
        )
        htorch.hpu.synchronize()
        return
    
    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: List[torch.Tensor],
        attn_metadata: AttentionMetadata,
    ) -> torch.Tensor:
        out = self.graph_model(
            input_ids,
            positions,
            kv_caches,
            attn_metadata.slot_mapping,
            attn_metadata.context_lens, 
            attn_metadata.block_tables,
        )
        return out


    def __call__(self, *args, **kwargs):
        return self.forward(*args, **kwargs)


def _get_graph_batch_size(batch_size: int) -> int:
    """Returns the padded batch size given actual batch size.

    Batch sizes are 1, 2, 4, _BATCH_SIZE_ALIGNMENT,
    2*_BATCH_SIZE_ALIGNMENT, 3*_BATCH_SIZE_ALIGNMENT...
    """
    if batch_size <= 2:
        return batch_size
    elif batch_size <= 4:
        return 4
    elif batch_size <= 8:
        return 8
    else:
        return ((batch_size + _BATCH_SIZE_ALIGNMENT - 1) //
                _BATCH_SIZE_ALIGNMENT * _BATCH_SIZE_ALIGNMENT)


def _get_graph_max_seq_len(max_seq_len: int) -> int:
    """Returns the padded batch size given actual batch size.

    Batch sizes are 1, 2, 4, _BATCH_SIZE_ALIGNMENT,
    2*_BATCH_SIZE_ALIGNMENT, 3*_BATCH_SIZE_ALIGNMENT...
    """
    if max_seq_len <= 32:
        return 32
    elif max_seq_len <= 64:
        return 64
    elif max_seq_len <= 128:
        return 128
    else:
        return ((max_seq_len + _MAX_SEQ_LEN_ALIGNMENT - 1) //
                _MAX_SEQ_LEN_ALIGNMENT * _MAX_SEQ_LEN_ALIGNMENT)