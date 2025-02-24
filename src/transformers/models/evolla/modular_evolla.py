# coding=utf-8
# Copyright 2025 EleutherAI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
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
"""PyTorch Evolla model."""

from dataclasses import dataclass
from typing import Optional, Tuple, Union

import torch
import torch.utils.checkpoint
from torch import nn

from ...cache_utils import Cache, DynamicCache, StaticCache
from ...generation import GenerationMixin
from ...modeling_attn_mask_utils import AttentionMaskConverter
from ...modeling_outputs import BaseModelOutputWithPast, CausalLMOutputWithPast, ModelOutput
from ...modeling_utils import PreTrainedModel
from ...pytorch_utils import ALL_LAYERNORM_LAYERS
from ...utils import (
    add_start_docstrings,
    add_start_docstrings_to_model_forward,
    logging,
)
from ..llama.modeling_llama import LlamaAttention, LlamaDecoderLayer, LlamaMLP, LlamaRMSNorm, LlamaRotaryEmbedding
from .configuration_evolla import EvollaConfig
from .protein import ProteinEncoderModelOutput, SaProtProteinEncoder
from .sequence_aligner import CrossAttention
from .sequence_compressor import SequenceCompressorResampler


logger = logging.get_logger(__name__)

_CONFIG_FOR_DOC = "EvollaConfig"


@dataclass
# this was adapted from transformers.models.idefics.modeling_idefics.IdeficsBaseModelOutputWithPast with Idefics->Evolla
class EvollaBaseModelOutputWithPast(ModelOutput):
    """
    Base class for Evolla model's outputs that may also contain a past key/values (to speed up sequential decoding).

    Args:
        last_hidden_state (`torch.FloatTensor` of shape `(batch_size, sequence_length, hidden_size)`):
            Sequence of hidden-states at the output of the last layer of the model.

            If `past_key_values` is used only the last hidden-state of the sequences of shape `(batch_size, 1,
            hidden_size)` is output.
        past_key_values (`tuple(tuple(torch.FloatTensor))`, *optional*, returned when `use_cache=True` is passed or when `config.use_cache=True`):
            Tuple of `tuple(torch.FloatTensor)` of length `config.n_layers`, with each tuple having 2 tensors of shape
            `(batch_size, num_heads, sequence_length, embed_size_per_head)`) and optionally if
            `config.is_encoder_decoder=True` 2 additional tensors of shape `(batch_size, num_heads,
            encoder_sequence_length, embed_size_per_head)`.

            Contains pre-computed hidden-states (key and values in the self-attention blocks and optionally if
            `config.is_encoder_decoder=True` in the cross-attention blocks) that can be used (see `past_key_values`
            input) to speed up sequential decoding.
        hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings, if the model has an embedding layer, +
            one for the output of each layer) of shape `(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the optional initial embedding outputs.
        attentions (`tuple(torch.FloatTensor)`, *optional*, returned when `output_attentions=True` is passed or when `config.output_attentions=True`):
            Tuple of `torch.FloatTensor` (one for each layer) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`.

            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.
        image_hidden_states (`tuple(torch.FloatTensor)`, *optional*):
            Tuple of `torch.FloatTensor` (one for the output of the image embeddings, `(batch_size, num_images,
            sequence_length, hidden_size)`.

            image_hidden_states of the model produced by the vision encoder, and optionally by the perceiver
    """

    last_hidden_state: torch.FloatTensor = None
    past_key_values: Optional[Tuple[Tuple[torch.FloatTensor]]] = None
    hidden_states: Optional[Tuple[torch.FloatTensor]] = None
    attentions: Optional[Tuple[torch.FloatTensor]] = None
    # protein_hidden_states: Optional[Tuple[torch.FloatTensor]] = None


def freeze_model(model, module_exceptions=[]):
    mapping = {
        "LayerNorm": nn.LayerNorm,
        "Linear": nn.Linear,
        "Embedding": nn.Embedding,
    }
    module_exceptions_mapped = [mapping[m] for m in module_exceptions]
    for module in model.modules():
        if module_exceptions and any(isinstance(module, t) for t in module_exceptions_mapped):
            module.requires_grad_(True)  # Explicitely setting it to true to avoid any mistakes
        else:
            module.requires_grad_(False)
    return model


class EvollaRMSNorm(LlamaRMSNorm):
    pass


ALL_LAYERNORM_LAYERS.append(EvollaRMSNorm)


class EvollaRotaryEmbedding(LlamaRotaryEmbedding):
    def __init__(self, config: EvollaConfig, device=None):
        super().__init__(config, device=device)
        # to pass `utils/check_config_attributes.py`
        self.max_position_embeddings = config.max_position_embeddings
        self.rope_scaling = config.rope_scaling


class EvollaMLP(LlamaMLP):
    pass


class EvollaAttention(LlamaAttention):
    def __init__(
        self,
        config: EvollaConfig,
        layer_idx: int,
    ):
        super().__init__(config, layer_idx)
        assert (
            self.num_key_value_groups > 0
        ), f"In {self._get_name()} num_attention_heads ({config.num_attention_heads}) must be larger than num_key_value_heads ({config.num_key_value_heads})"


# this was adapted from LlamaDecoderLayer
class EvollaDecoderLayer(LlamaDecoderLayer):
    def __init__(
        self,
        config: EvollaConfig,
        layer_idx: int,
    ):
        super().__init__()
        self.config = config
        self.hidden_size = config.hidden_size

        self.self_attn = EvollaAttention(
            config=config,
            layer_idx=layer_idx,
        )

        self.mlp = EvollaMLP(config=config)
        self.input_layernorm = EvollaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = EvollaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)


LLAMA_START_DOCSTRING = r"""
    This model inherits from [`PreTrainedModel`]. Check the superclass documentation for the generic methods the
    library implements for all its model (such as downloading or saving, resizing the input embeddings, pruning heads
    etc.)

    This model is also a PyTorch [torch.nn.Module](https://pytorch.org/docs/stable/nn.html#torch.nn.Module) subclass.
    Use it as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to general usage
    and behavior.

    Parameters:
        config ([`EvollaConfig`]):
            Model configuration class with all the parameters of the model. Initializing with a config file does not
            load the weights associated with the model, only the configuration. Check out the
            [`~PreTrainedModel.from_pretrained`] method to load the model weights.
"""


@add_start_docstrings(
    "The bare LLaMA Model outputting raw hidden-states without any specific head on top.",
    LLAMA_START_DOCSTRING,
)
# this was adapted from transformers.models.idefics.modeling_idefics.IdeficsPreTrainedModel with Idefics->Evolla
class EvollaPreTrainedModel(PreTrainedModel):
    config_class = EvollaConfig
    base_model_prefix = "model"
    supports_gradient_checkpointing = True
    _no_split_modules = ["EvollaDecoderLayer", "CrossAttention"]
    _supports_sdpa = True
    _supports_cache_class = True
    _supports_static_cache = True

    def _init_weights(self, module):
        # important: this ported version of Evolla isn't meant for training from scratch - only
        # inference and fine-tuning - so the proper init weights code has been removed - the m4 code
        # base should be used for training from scratch and it contains the correct code.
        std = self.config.initializer_range
        if isinstance(module, nn.Linear):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()


class EvollaProteinEncoder(nn.Module):
    def __init__(
        self,
        vocab_size: int,
        mask_token_id: int,
        pad_token_id: int,
        hidden_size: int,
        num_hidden_layers: int,
        num_attention_heads: int,
        intermediate_size: int,
        hidden_dropout_prob: float,
        attention_probs_dropout_prob: float,
        max_position_embeddings: int,
        layer_norm_eps: float,
        position_embedding_type: str,
        emb_layer_norm_before: bool,
        token_dropout: bool,
        resampler_output_repr_dim: int,
        resampler_depth: int,
        resampler_dim_head: int,
        resampler_heads: int,
        resampler_num_latents: int,
        resampler_ff_mult: int,
        add_pooling_layer: bool = False,
    ):
        super().__init__()
        self.model = SaProtProteinEncoder(
            vocab_size=vocab_size,
            mask_token_id=mask_token_id,
            pad_token_id=pad_token_id,
            hidden_size=hidden_size,
            num_hidden_layers=num_hidden_layers,
            num_attention_heads=num_attention_heads,
            intermediate_size=intermediate_size,
            hidden_dropout_prob=hidden_dropout_prob,
            attention_probs_dropout_prob=attention_probs_dropout_prob,
            max_position_embeddings=max_position_embeddings,
            layer_norm_eps=layer_norm_eps,
            position_embedding_type=position_embedding_type,
            emb_layer_norm_before=emb_layer_norm_before,
            token_dropout=token_dropout,
            add_pooling_layer=add_pooling_layer,
        )
        self.sequence_compressor_resampler = SequenceCompressorResampler(
            protein_repr_dim=hidden_size,
            output_repr_dim=resampler_output_repr_dim,
            depth=resampler_depth,
            dim_head=resampler_dim_head,
            heads=resampler_heads,
            num_latents=resampler_num_latents,
            ff_mult=resampler_ff_mult,
        )

    def sequence_encode(
        self,
        input_ids: torch.LongTensor,
        attention_mask: torch.FloatTensor,
        return_dict: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
    ):
        sequence_repr = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            return_dict=return_dict,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
        )

        return sequence_repr

    def forward(
        self,
        input_ids: torch.LongTensor,
        attention_mask: torch.FloatTensor,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        **kwargs,
    ):
        protein_output = self.sequence_encode(
            input_ids=input_ids,
            attention_mask=attention_mask,
            return_dict=True,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
        )

        # TODO: could be replaced by last hidden state
        protein_embeds = protein_output.last_hidden_state

        sequence_repr = self.sequence_compressor_resampler(protein_embeds, attention_mask)

        if not return_dict:
            return sequence_repr, protein_embeds, attention_mask

        return ProteinEncoderModelOutput(
            sequence_compressor_output=sequence_repr,
            last_hidden_state=protein_output.last_hidden_state,
            hidden_states=protein_output.hidden_states,
            attentions=protein_output.attentions,
        )


LLAMA_INPUTS_DOCSTRING = r"""
    Args:
        input_ids (`torch.LongTensor` of shape `(batch_size, sequence_length)`):
            Indices of input sequence tokens in the vocabulary. Padding will be ignored by default should you provide
            it.

            Indices can be obtained using [`AutoTokenizer`]. See [`PreTrainedTokenizer.encode`] and
            [`PreTrainedTokenizer.__call__`] for details.

            [What are input IDs?](../glossary#input-ids)
        attention_mask (`torch.Tensor` of shape `(batch_size, sequence_length)`, *optional*):
            Mask to avoid performing attention on padding token indices. Mask values selected in `[0, 1]`:

            - 1 for tokens that are **not masked**,
            - 0 for tokens that are **masked**.

            [What are attention masks?](../glossary#attention-mask)

            Indices can be obtained using [`AutoTokenizer`]. See [`PreTrainedTokenizer.encode`] and
            [`PreTrainedTokenizer.__call__`] for details.

            If `past_key_values` is used, optionally only the last `decoder_input_ids` have to be input (see
            `past_key_values`).

            If you want to change padding behavior, you should read [`modeling_opt._prepare_decoder_attention_mask`]
            and modify to your needs. See diagram 1 in [the paper](https://arxiv.org/abs/1910.13461) for more
            information on the default strategy.

            - 1 indicates the head is **not masked**,
            - 0 indicates the head is **masked**.
        position_ids (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
            Indices of positions of each input sequence tokens in the position embeddings. Selected in the range `[0,
            config.n_positions - 1]`. [What are position IDs?](../glossary#position-ids)
        past_key_values (`tuple(tuple(torch.FloatTensor))`, *optional*, returned when `use_cache=True` is passed or when `config.use_cache=True`):
            Tuple of `tuple(torch.FloatTensor)` of length `config.n_layers`, with each tuple having 2 tensors of shape
            `(batch_size, num_heads, sequence_length, embed_size_per_head)`) and 2 additional tensors of shape
            `(batch_size, num_heads, encoder_sequence_length, embed_size_per_head)`.

            Contains pre-computed hidden-states (key and values in the self-attention blocks and in the cross-attention
            blocks) that can be used (see `past_key_values` input) to speed up sequential decoding.

            If `past_key_values` are used, the user can optionally input only the last `decoder_input_ids` (those that
            don't have their past key value states given to this model) of shape `(batch_size, 1)` instead of all
            `decoder_input_ids` of shape `(batch_size, sequence_length)`.
        inputs_embeds (`torch.FloatTensor` of shape `(batch_size, sequence_length, hidden_size)`, *optional*):
            Optionally, instead of passing `input_ids` you can choose to directly pass an embedded representation. This
            is useful if you want more control over how to convert `input_ids` indices into associated vectors than the
            model's internal embedding lookup matrix.
        use_cache (`bool`, *optional*):
            If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding (see
            `past_key_values`).
        output_attentions (`bool`, *optional*):
            Whether or not to return the attentions tensors of all attention layers. See `attentions` under returned
            tensors for more detail.
        output_hidden_states (`bool`, *optional*):
            Whether or not to return the hidden states of all layers. See `hidden_states` under returned tensors for
            more detail.
        return_dict (`bool`, *optional*):
            Whether or not to return a [`~utils.ModelOutput`] instead of a plain tuple.
        cache_position (`torch.LongTensor` of shape `(sequence_length)`, *optional*):
            Indices depicting the position of the input sequence tokens in the sequence. Contrarily to `position_ids`,
            this tensor is not affected by padding. It is used to update the cache in the correct position and to infer
            the complete sequence length.
"""


class EvollaLLM(nn.Module):
    """
    Transformer decoder consisting of *config.num_hidden_layers* layers. Each layer is a [`LlamaDecoderLayer`]

    Args:
        config: LlamaConfig
    """

    def __init__(
        self,
        config: EvollaConfig,
        vocab_size: int,
        pad_token_id: int,
        num_hidden_layers: int,
        num_add_layers: int,
        aligner_num_attention_heads: int,
        aligner_attention_probs_dropout_prob: float,
        aligner_enable_bias: bool,
        aligner_ffn_mult: int,
        protein_encoder_dim: int,
    ):
        super().__init__()
        self.padding_idx = pad_token_id
        self.vocab_size = vocab_size

        self.embed_tokens = nn.Embedding(vocab_size, config.hidden_size, self.padding_idx)
        self.layers = nn.ModuleList(
            [
                EvollaDecoderLayer(
                    config=config,
                    layer_idx=layer_idx,
                )
                for layer_idx in range(num_hidden_layers)
            ]
        )
        every_n_layers = max(num_hidden_layers // num_add_layers, 1)
        for i, layer in enumerate(range(num_hidden_layers)):
            if (i + 1) % every_n_layers == 0:
                self.layers[i].adapter = CrossAttention(
                    hidden_size=config.hidden_size,
                    num_attention_heads=aligner_num_attention_heads,
                    attention_probs_dropout_prob=aligner_attention_probs_dropout_prob,
                    enable_bias=aligner_enable_bias,
                    ffn_mult=aligner_ffn_mult,
                    protein_encoder_dim=protein_encoder_dim,
                )

        self.norm = EvollaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.rotary_emb = EvollaRotaryEmbedding(config=config)
        self.gradient_checkpointing = False

        # self.use_cache = config.use_cache
        # self.use_return_dict = config.use_return_dict
        self.config = config

        # Initialize weights and apply final processing
        # self.post_init()

    def get_input_embeddings(self):
        return self.embed_tokens

    def set_input_embeddings(self, value):
        self.embed_tokens = value

    @add_start_docstrings_to_model_forward(LLAMA_INPUTS_DOCSTRING)
    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[Cache] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        cache_position: Optional[torch.LongTensor] = None,
        protein_feats: Optional[torch.FloatTensor] = None,
        structure_feats: Optional[torch.FloatTensor] = None,
        msa_feats: Optional[torch.FloatTensor] = None,
        protein_batch_mask: Optional[torch.Tensor] = None,
        structure_batch_mask: Optional[torch.Tensor] = None,
        msa_batch_mask: Optional[torch.Tensor] = None,
        **kwargs,
    ) -> Union[Tuple, BaseModelOutputWithPast]:
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.return_dict

        if (input_ids is None) ^ (inputs_embeds is not None):
            raise ValueError("You must specify exactly one of input_ids or inputs_embeds")

        if self.gradient_checkpointing and self.training and use_cache:
            logger.warning_once(
                "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`."
            )
            use_cache = False

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)

        batch_size, seq_length, _ = inputs_embeds.shape
        past_key_values_length = past_key_values.get_seq_length() if past_key_values is not None else 0
        seq_length_with_past = seq_length + past_key_values_length

        if use_cache and past_key_values is None:
            past_key_values = DynamicCache()

        if attention_mask is None:
            attention_mask = torch.ones(
                (batch_size, seq_length_with_past), dtype=torch.int64, device=inputs_embeds.device
            )

        if cache_position is None:
            past_seen_tokens = past_key_values.get_seq_length() if past_key_values is not None else 0
            cache_position = torch.arange(
                past_seen_tokens, past_seen_tokens + inputs_embeds.shape[1], device=inputs_embeds.device
            )

        if position_ids is None:
            position_ids = cache_position.unsqueeze(0)

        causal_mask = self._update_causal_mask(
            attention_mask, inputs_embeds, cache_position, past_key_values, output_attentions
        )

        hidden_states = inputs_embeds

        # create position embeddings to be shared across the decoder layers
        position_embeddings = self.rotary_emb(hidden_states, position_ids)

        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None

        for decoder_layer in self.layers:
            if output_hidden_states:
                all_hidden_states += (hidden_states,)

            if self.gradient_checkpointing and self.training:
                if not hasattr(decoder_layer, "adapter"):
                    layer_outputs = self._gradient_checkpointing_func(
                        decoder_layer.__call__,
                        hidden_states,
                        causal_mask,
                        position_ids,
                        past_key_values,
                        output_attentions,
                        use_cache,
                        cache_position,
                        position_embeddings,
                    )
                else:
                    layer_outputs = self._gradient_checkpointing_func(
                        decoder_layer.__call__,
                        hidden_states,
                        causal_mask,
                        position_ids,
                        past_key_values,
                        output_attentions,
                        use_cache,
                        cache_position,
                        position_embeddings,
                    )
                    # keep the hidden_states only, cache other outputs
                    hidden_states = layer_outputs[0]
                    other_outputs = layer_outputs[1:]
                    hidden_states = decoder_layer.adapter(
                        query_states=hidden_states,
                        protein_kv_states=protein_feats,
                        structure_kv_states=structure_feats,
                        msa_kv_states=msa_feats,
                        protein_batch_mask=protein_batch_mask,
                        structure_batch_mask=structure_batch_mask,
                        msa_batch_mask=msa_batch_mask,
                        query_attn_mask=attention_mask,
                    )
                    layer_outputs = (hidden_states,) + other_outputs
            else:
                if not hasattr(decoder_layer, "adapter"):
                    layer_outputs = decoder_layer(
                        hidden_states,
                        attention_mask=causal_mask,
                        position_ids=position_ids,
                        past_key_value=past_key_values,
                        output_attentions=output_attentions,
                        use_cache=use_cache,
                        cache_position=cache_position,
                        position_embeddings=position_embeddings,
                        **kwargs,
                    )
                else:
                    layer_outputs = decoder_layer(
                        hidden_states,
                        attention_mask=causal_mask,
                        position_ids=position_ids,
                        past_key_value=past_key_values,
                        output_attentions=output_attentions,
                        use_cache=use_cache,
                        cache_position=cache_position,
                        position_embeddings=position_embeddings,
                        **kwargs,
                    )

                    # keep the hidden_states only, cache other outputs
                    hidden_states = layer_outputs[0]
                    other_outputs = layer_outputs[1:]
                    hidden_states = decoder_layer.adapter(
                        query_states=hidden_states,
                        protein_kv_states=protein_feats,
                        structure_kv_states=structure_feats,
                        msa_kv_states=msa_feats,
                        protein_batch_mask=protein_batch_mask,
                        structure_batch_mask=structure_batch_mask,
                        msa_batch_mask=msa_batch_mask,
                        query_attn_mask=attention_mask,
                    )
                    layer_outputs = (hidden_states,) + other_outputs

            hidden_states = layer_outputs[0]

            if output_attentions:
                all_self_attns += (layer_outputs[1],)

        hidden_states = self.norm(hidden_states)

        # add hidden states from the last decoder layer
        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        output = BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=past_key_values if use_cache else None,
            hidden_states=all_hidden_states,
            attentions=all_self_attns,
        )
        return output if return_dict else output.to_tuple()

    def _update_causal_mask(
        self,
        attention_mask: torch.Tensor,
        input_tensor: torch.Tensor,
        cache_position: torch.Tensor,
        past_key_values: Cache,
        output_attentions: bool,
    ):
        if self.config._attn_implementation == "flash_attention_2":
            if attention_mask is not None and (attention_mask == 0.0).any():
                return attention_mask
            return None

        # For SDPA, when possible, we will rely on its `is_causal` argument instead of its `attn_mask` argument, in
        # order to dispatch on Flash Attention 2. This feature is not compatible with static cache, as SDPA will fail
        # to infer the attention mask.
        past_seen_tokens = past_key_values.get_seq_length() if past_key_values is not None else 0
        using_static_cache = isinstance(past_key_values, StaticCache)

        # When output attentions is True, sdpa implementation's forward method calls the eager implementation's forward
        if self.config._attn_implementation == "sdpa" and not using_static_cache and not output_attentions:
            if AttentionMaskConverter._ignore_causal_mask_sdpa(
                attention_mask,
                inputs_embeds=input_tensor,
                past_key_values_length=past_seen_tokens,
                is_training=self.training,
            ):
                return None

        dtype, device = input_tensor.dtype, input_tensor.device
        sequence_length = input_tensor.shape[1]
        if using_static_cache:
            target_length = past_key_values.get_max_cache_shape()
        else:
            target_length = (
                attention_mask.shape[-1]
                if isinstance(attention_mask, torch.Tensor)
                else past_seen_tokens + sequence_length + 1
            )

        # In case the provided `attention` mask is 2D, we generate a causal mask here (4D).
        causal_mask = self._prepare_4d_causal_attention_mask_with_cache_position(
            attention_mask,
            sequence_length=sequence_length,
            target_length=target_length,
            dtype=dtype,
            device=device,
            cache_position=cache_position,
            batch_size=input_tensor.shape[0],
        )

        if (
            self.config._attn_implementation == "sdpa"
            and attention_mask is not None
            and attention_mask.device.type == "cuda"
            and not output_attentions
        ):
            # Attend to all tokens in fully masked rows in the causal_mask, for example the relevant first rows when
            # using left padding. This is required by F.scaled_dot_product_attention memory-efficient attention path.
            # Details: https://github.com/pytorch/pytorch/issues/110213
            min_dtype = torch.finfo(dtype).min
            causal_mask = AttentionMaskConverter._unmask_unattended(causal_mask, min_dtype)

        return causal_mask

    @staticmethod
    def _prepare_4d_causal_attention_mask_with_cache_position(
        attention_mask: torch.Tensor,
        sequence_length: int,
        target_length: int,
        dtype: torch.dtype,
        device: torch.device,
        cache_position: torch.Tensor,
        batch_size: int,
        **kwargs,
    ):
        """
        Creates a causal 4D mask of shape `(batch_size, 1, query_length, key_value_length)` from a 2D mask of shape
        `(batch_size, key_value_length)`, or if the input `attention_mask` is already 4D, do nothing.

        Args:
            attention_mask (`torch.Tensor`):
                A 2D attention mask of shape `(batch_size, key_value_length)` or a 4D attention mask of shape
                `(batch_size, 1, query_length, key_value_length)`.
            sequence_length (`int`):
                The sequence length being processed.
            target_length (`int`):
                The target length: when generating with static cache, the mask should be as long as the static cache,
                to account for the 0 padding, the part of the cache that is not filled yet.
            dtype (`torch.dtype`):
                The dtype to use for the 4D attention mask.
            device (`torch.device`):
                The device to plcae the 4D attention mask on.
            cache_position (`torch.Tensor`):
                Indices depicting the position of the input sequence tokens in the sequence.
            batch_size (`torch.Tensor`):
                Batch size.
        """
        if attention_mask is not None and attention_mask.dim() == 4:
            # In this case we assume that the mask comes already in inverted form and requires no inversion or slicing.
            causal_mask = attention_mask
        else:
            min_dtype = torch.finfo(dtype).min
            causal_mask = torch.full(
                (sequence_length, target_length), fill_value=min_dtype, dtype=dtype, device=device
            )
            if sequence_length != 1:
                causal_mask = torch.triu(causal_mask, diagonal=1)
            causal_mask *= torch.arange(target_length, device=device) > cache_position.reshape(-1, 1)
            causal_mask = causal_mask[None, None, :, :].expand(batch_size, 1, -1, -1)
            if attention_mask is not None:
                causal_mask = causal_mask.clone()  # copy to contiguous memory for in-place edit
                mask_length = attention_mask.shape[-1]
                padding_mask = causal_mask[:, :, :, :mask_length] + attention_mask[:, None, None, :]
                padding_mask = padding_mask == 0
                causal_mask[:, :, :, :mask_length] = causal_mask[:, :, :, :mask_length].masked_fill(
                    padding_mask, min_dtype
                )

        return causal_mask


class EvollaModel(EvollaPreTrainedModel):
    r""" """

    def __init__(self, config: EvollaConfig, **kwargs):
        super().__init__(config)
        self.config = config
        self.gradient_checkpointing = getattr(config, "gradient_checkpointing", False)

        self.initialize_model()

    def initialize_model(self):
        self.protein_encoder = EvollaProteinEncoder(
            vocab_size=self.config.protein_vocab_size,
            mask_token_id=self.config.protein_mask_token_id,
            pad_token_id=self.config.protein_pad_token_id,
            hidden_size=self.config.protein_hidden_size,
            num_hidden_layers=self.config.protein_num_hidden_layers,
            num_attention_heads=self.config.protein_num_attention_heads,
            intermediate_size=self.config.protein_intermediate_size,
            hidden_dropout_prob=self.config.protein_hidden_dropout_prob,
            attention_probs_dropout_prob=self.config.protein_attention_probs_dropout_prob,
            max_position_embeddings=self.config.protein_max_position_embeddings,
            layer_norm_eps=self.config.protein_layer_norm_eps,
            position_embedding_type=self.config.protein_position_embedding_type,
            emb_layer_norm_before=self.config.protein_emb_layer_norm_before,
            token_dropout=self.config.protein_token_dropout,
            resampler_output_repr_dim=self.config.hidden_size,
            resampler_depth=self.config.resampler_depth,
            resampler_dim_head=self.config.resampler_dim_head,
            resampler_heads=self.config.resampler_heads,
            resampler_num_latents=self.config.resampler_num_latents,
            resampler_ff_mult=self.config.resampler_ff_mult,
            add_pooling_layer=False,
        )

        self.llm = EvollaLLM(
            config=self.config,
            vocab_size=self.config.vocab_size,
            pad_token_id=self.config.pad_token_id,
            num_hidden_layers=self.config.num_hidden_layers,
            num_add_layers=self.config.aligner_num_add_layers,
            aligner_num_attention_heads=self.config.num_attention_heads,
            aligner_attention_probs_dropout_prob=self.config.aligner_attention_probs_dropout_prob,
            aligner_enable_bias=self.config.aligner_enable_bias,
            aligner_ffn_mult=self.config.aligner_ffn_mult,
            protein_encoder_dim=self.config.hidden_size,
        )

        self.post_init()

    def forward(
        self,
        input_ids: torch.LongTensor = None,  # text input ids
        attention_mask: Optional[torch.Tensor] = None,  # text attention mask
        inputs_embeds: Optional[torch.FloatTensor] = None,  # text input embeddings
        protein_input_ids: torch.LongTensor = None,
        protein_attention_mask: Optional[torch.Tensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        use_cache: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        **kwargs,
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.return_dict

        if (input_ids is None) ^ (inputs_embeds is not None):
            raise ValueError("You must specify exactly one of input_ids or inputs_embeds")

        if protein_input_ids is None:
            raise ValueError("protein_input_ids is required")

        text_input_ids = input_ids
        text_attention_mask = attention_mask
        text_inputs_embeds = inputs_embeds

        # create batch mask for seqs
        protein_batch_mask = torch.tensor([True] * protein_input_ids.shape[0])

        protein_outputs = self.protein_encoder(
            input_ids=protein_input_ids,
            attention_mask=protein_attention_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=True,
        )

        text_outputs = self.llm(
            input_ids=text_input_ids,
            attention_mask=text_attention_mask,
            inputs_embeds=text_inputs_embeds,
            protein_feats=protein_outputs.sequence_compressor_output,
            protein_batch_mask=protein_batch_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=True,
        )

        if output_hidden_states:
            decoder_hidden_states = text_outputs.hidden_states
        else:
            decoder_hidden_states = None

        last_hidden_state = text_outputs.last_hidden_state

        if output_attentions:
            decoder_attentions = text_outputs.attentions
        else:
            decoder_attentions = None

        # change the output to BaseModelOutputWithPast
        output = EvollaBaseModelOutputWithPast(
            last_hidden_state=last_hidden_state,
            hidden_states=decoder_hidden_states,
            attentions=decoder_attentions,
            # protein_hidden_states=encoder_hidden_states,
        )
        return output if return_dict else output.to_tuple()

    def embed_tokens(self, input_ids, **kwargs):
        return self.llm.embed_tokens(input_ids, **kwargs)

    def get_input_embeddings(self):
        return self.llm.get_input_embeddings()

    def set_input_embeddings(self, value):
        self.llm.set_input_embeddings(value)


# this was adapted from modeling_idefics.IdeficsForVisionText2Text
class EvollaForProteinText2Text(EvollaPreTrainedModel, GenerationMixin):
    _keys_to_ignore_on_load_missing = [r"lm_head.weight"]
    _tied_weights_keys = []

    def __init__(self, config):
        super().__init__(config)
        self.model = EvollaModel(config)
        self.vocab_size = config.vocab_size
        self.lm_head = nn.Linear(config.hidden_size, self.vocab_size, bias=False)

        self.post_init()

    def get_input_embeddings(self):
        return self.model.get_input_embeddings()

    def set_input_embeddings(self, value):
        return self.model.set_input_embeddings(value)

    def forward(
        self,
        input_ids: torch.LongTensor = None,  # text input ids
        attention_mask: Optional[torch.Tensor] = None,  # text attention mask
        inputs_embeds: Optional[torch.FloatTensor] = None,  # text input embeddings
        labels: Optional[torch.LongTensor] = None,
        protein_input_ids: torch.LongTensor = None,
        protein_attention_mask: Optional[torch.Tensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        use_cache: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        **kwargs,
    ):
        r"""
        Args:

        Returns:

        Example:

        ```python
        >>> from transformers import EvollaProcessor, EvollaForProteinText2Text
        >>> model = EvollaForProteinText2Text.from_pretrained("westlake/Evolla-10B-hf")
        >>> processor = EvollaProcessor.from_pretrained("westlake/Evolla-10B-hf")

        >>> protein_information = {
            "aa_seq": "your amino acid sequence",
            "foldseek": "your foldseek sequence",
        }
        >>> question = "What is the function of this protein?"
        >>> message = [
            {"role": "system", "content": "You are an AI expert that can answer any questions about protein."},
            {"role": "user", "content": question},
        ]

        >>> inputs = processor(proteins=[protein_information], messages_list=[message], return_tensors="pt", padding="longest")
        >>> outputs = model.generate(**inputs)

        >>> print(processor.batch_decode(outputs, skip_special_tokens=True))
        ```"""
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            inputs_embeds=inputs_embeds,
            protein_input_ids=protein_input_ids,
            protein_attention_mask=protein_attention_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            use_cache=use_cache,
            return_dict=True,
            **kwargs,
        )
        hidden_states = outputs[0]
        logits = self.lm_head(hidden_states)

        loss = None
        if labels is not None:
            loss = self.loss_function(logits=logits, labels=labels, vocab_size=self.vocab_size, **kwargs)

        lm_outputs = CausalLMOutputWithPast(
            loss=loss,
            logits=logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )
        return lm_outputs if return_dict else lm_outputs.to_tuple()


__all__ = ["EvollaForProteinText2Text", "EvollaModel", "EvollaPreTrainedModel"]
