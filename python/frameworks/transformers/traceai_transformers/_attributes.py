"""Bounded, text-only extraction; tensors, media and arbitrary objects are not serialized."""

import json
import math
from collections.abc import Mapping
from typing import Any, Dict, Optional

MAX_ITEMS = 128
MAX_TEXT = 65536
MAX_TOKEN_IDS = 65536
PARAMETERS = (
    "max_new_tokens", "max_length", "min_new_tokens", "min_length", "temperature",
    "top_p", "top_k", "typical_p", "do_sample", "num_beams", "num_return_sequences",
    "repetition_penalty", "length_penalty", "early_stopping", "use_cache", "seed",
)


def text(value: Any) -> Optional[str]:
    # Normalize accepted string subclasses to plain text without invoking an
    # overridden __str__ or __getitem__. Prefix attribution must match HF's
    # isinstance-based chat acceptance, including when input capture is hidden.
    return str.__getitem__(value, slice(None, MAX_TEXT)) if isinstance(value, str) else None


def model_name(instance: Any, pipeline: bool) -> Optional[str]:
    model = getattr(instance, "model", None) if pipeline else instance
    for obj, key in ((getattr(model, "config", None), "_name_or_path"), (model, "name_or_path")):
        name = getattr(obj, key, None)
        if type(name) is str and name:
            return name[:1024]
    return None


def parameters(instance: Any, kwargs: dict, pipeline: bool, args: tuple = ()) -> dict:
    model = getattr(instance, "model", None) if pipeline else instance
    effective = dict(getattr(instance, "_forward_params", {})) if pipeline else {}
    effective.update(kwargs)
    config = effective.get("generation_config")
    if config is None and not pipeline and len(args) > 1:
        config = args[1]
    if config is None:
        config = getattr(instance, "generation_config", None)
    if config is None:
        config = getattr(model, "generation_config", None)
    result = {}
    # HF merges call kwargs over pipeline defaults, then applies the merged
    # explicit values over the selected (pipeline or call) GenerationConfig.
    for source in (config, effective):
        for key in PARAMETERS:
            value = source.get(key) if isinstance(source, Mapping) else getattr(source, key, None)
            if type(value) in (str, bool, int, float) and (type(value) is not float or math.isfinite(value)):
                result[key] = value[:1024] if type(value) is str else value
    return result


def parameter_attributes(params: dict) -> dict:
    if not params:
        return {}
    attrs = {"gen_ai.request.parameters": json.dumps(params, separators=(",", ":"))}
    for source, target in (("temperature", "temperature"), ("top_p", "top_p"), ("top_k", "top_k"), ("seed", "seed")):
        if source in params:
            attrs["gen_ai.request." + target] = params[source]
    if "max_new_tokens" in params:
        attrs["gen_ai.request.max_tokens"] = params["max_new_tokens"]
    return attrs


def _content(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return text(value)
    if type(value) is list:
        # Chat templates can accept multimodal messages. Only explicitly textual
        # parts are captured; image/audio URLs, bytes and arrays are not inspected.
        parts = [text(part.get("text")) for part in value[:MAX_ITEMS]
                 if isinstance(part, Mapping) and part.get("type") in ("text", "input_text", "output_text")]
        return "\n".join(part for part in parts if part is not None)[:MAX_TEXT]
    return None


def _message(value: Mapping) -> Optional[dict]:
    role = text(value.get("role"))
    content = _content(value.get("content"))
    if role is not None and content is not None:
        return {"role": role[:64], "content": content}
    return None


def messages(value: Any) -> list:
    if isinstance(value, str):
        return [{"role": "user", "content": text(value)}]
    if type(value) is not list:
        return []
    out = []
    for item in value[:MAX_ITEMS]:
        if isinstance(item, Mapping):
            message = _message(item)
            if message is not None:
                out.append(message)
        elif isinstance(item, str):
            out.append({"role": "user", "content": text(item)})
        elif type(item) is list:
            # Batched chat: keep a bounded flattened representation.
            for value in item[:MAX_ITEMS]:
                if isinstance(value, Mapping):
                    message = _message(value)
                    if message is not None:
                        out.append(message)
                if len(out) >= MAX_ITEMS:
                    break
        if len(out) >= MAX_ITEMS:
            break
    return out[:MAX_ITEMS]


def message_attributes(items: list, side: str) -> dict:
    if not items:
        return {}
    attrs = {side + ".mime_type": "application/json"}
    bounded = []
    budget = MAX_TEXT
    for index, item in enumerate(items[:MAX_ITEMS]):
        prefix = "gen_ai." + side + ".messages." + str(index) + ".message."
        attrs[prefix + "role"] = item["role"][:64]
        attrs[prefix + "content"] = item["content"][:budget]
        bounded.append({"role": attrs[prefix + "role"], "content": attrs[prefix + "content"]})
        budget -= len(attrs[prefix + "content"])
        if budget <= 0:
            break
    encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))
    # Keep the aggregate valid JSON even for long/control-character-heavy text.
    while len(encoded) > MAX_TEXT and bounded:
        overflow = len(encoded) - MAX_TEXT
        if bounded[-1]["content"]:
            bounded[-1]["content"] = bounded[-1]["content"][:-overflow]
        else:
            bounded.pop()
        encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))
    attrs[side + ".value"] = encoded
    return attrs


def output_messages(result: Any, source: Any, kwargs: dict) -> list:
    records = result if type(result) is list else [result]
    out = []
    for index, entry in enumerate(records[:MAX_ITEMS]):
        entries = entry if type(entry) is list else [entry]
        for record in entries[:MAX_ITEMS]:
            if not isinstance(record, Mapping):
                continue
            value = record.get("generated_text", record.get("summary_text", record.get("translation_text")))
            prompt = source
            if type(source) is list and source and isinstance(source[0], (str, list)):
                prompt = source[min(index, len(source) - 1)]
            if isinstance(value, str):
                if isinstance(prompt, str) and kwargs.get("return_full_text", True) and str.startswith(value, prompt):
                    value = str.__getitem__(value, slice(str.__len__(prompt), None))
                out.append({"role": "assistant", "content": text(value)})
            elif type(value) is list:
                # Compare only normalized text, never arbitrary multimodal values.
                # HF can continue the final message in place regardless of role
                # (continue_final_message=True), including user/system/tool text.
                # Prior messages and the continued prefix are always input, even
                # when they occur in the pipeline's returned generated_text.
                generated = messages(value)
                prior = messages(prompt) if type(prompt) is list else []
                if prior and generated[:len(prior)] == prior:
                    generated = generated[len(prior):]
                elif (prior and len(generated) >= len(prior)
                      and generated[:len(prior) - 1] == prior[:-1]
                      and generated[len(prior) - 1]["role"] == prior[-1]["role"]
                      and generated[len(prior) - 1]["content"].startswith(prior[-1]["content"])):
                    continued = generated[len(prior) - 1]["content"][len(prior[-1]["content"]):]
                    generated = [{"role": prior[-1]["role"], "content": continued}] + generated[len(prior):]
                out.extend(generated)
            if len(out) >= MAX_ITEMS:
                return out[:MAX_ITEMS]
    return out[:MAX_ITEMS]


def matrix(value: Any) -> Optional[list]:
    """Copy only small numeric ID/mask tensors, never arbitrary user objects."""
    if type(value).__module__.split(".")[0] in ("torch", "numpy"):
        shape = value.shape
        if len(shape) not in (1, 2) or math.prod(shape) > MAX_TOKEN_IDS:
            return None
        value = value.tolist()
    if type(value) is not list or not value:
        return None
    if type(value[0]) is int:
        value = [value]
    if len(value) > MAX_ITEMS:
        return None
    total = 0
    rows = []
    for row in value:
        if type(row) is not list:
            return None
        total += len(row)
        if total > MAX_TOKEN_IDS or any(type(item) not in (int, bool) for item in row):
            return None
        rows.append(row)
    return rows


def generation_sequences(result: Any) -> Any:
    if isinstance(result, Mapping):
        return result.get("sequences")
    if type(result).__module__.startswith("transformers."):
        return getattr(result, "sequences", result)
    return result


def generation_attributes(instance: Any, args: tuple, kwargs: dict, result: Any, tokenizer: Any) -> dict:
    source = kwargs.get("input_ids", kwargs.get("inputs", args[0] if args else None))
    ids = matrix(source)
    mask = matrix(kwargs.get("attention_mask"))
    output = matrix(generation_sequences(result))
    config = getattr(instance, "config", None)
    encoder_decoder = getattr(config, "is_encoder_decoder", False) is True
    attrs: Dict[str, Any] = {}
    if mask is not None:
        attrs["gen_ai.usage.input_tokens"] = sum(sum(row) for row in mask)
    elif ids is not None:
        attrs["gen_ai.usage.input_tokens"] = sum(len(row) for row in ids)
    embedding_only = source is None and kwargs.get("inputs_embeds") is not None
    if output is None or (ids is None and not encoder_decoder and not embedding_only):
        return attrs
    # Decoder-only embedding prompts are not prepended to returned token IDs.
    prefix = 1 if encoder_decoder else len(ids[0]) if ids is not None else 0
    prefixes = [prefix] * len(output)
    if encoder_decoder:
        decoder_source = kwargs.get("decoder_input_ids")
        if decoder_source is None and getattr(instance, "main_input_name", "input_ids") != "input_ids":
            decoder_source = kwargs.get("input_ids")
        if decoder_source is not None:
            decoder_ids = matrix(decoder_source)
            # Unverifiable prompts must not become output, even when input
            # capture is hidden or the prompt exceeds our extraction limits.
            if not decoder_ids or len(output) % len(decoder_ids):
                return attrs
            repeats = len(output) // len(decoder_ids)
            for index, row in enumerate(output):
                prompt = decoder_ids[index // repeats]
                prefix = len(prompt)
                if not prompt:
                    return attrs
                # HF may prepend a decoder-start token. Compare the actual
                # returned prefix instead of assuming the caller included it;
                # beam/multiple-return sequences repeat each prompt in order.
                if row[:prefix] == prompt:
                    prefixes[index] = prefix
                elif row[1:prefix + 1] == prompt:
                    prefixes[index] = prefix + 1
                else:
                    return attrs
    generation_config = kwargs.get("generation_config")
    if generation_config is None:
        generation_config = args[1] if len(args) > 1 else getattr(instance, "generation_config", None)
    eos = kwargs.get("eos_token_id", getattr(generation_config, "eos_token_id", None))
    eos_ids = [eos] if type(eos) is int else eos if type(eos) is list else []
    generated = []
    for row, prefix in zip(output, prefixes):
        row = row[prefix:]
        for index, token in enumerate(row):
            if token in eos_ids:
                row = row[:index + 1]
                break
        generated.append(row)
    attrs["gen_ai.usage.output_tokens"] = sum(len(row) for row in generated)
    if "gen_ai.usage.input_tokens" in attrs:
        attrs["gen_ai.usage.total_tokens"] = attrs["gen_ai.usage.input_tokens"] + attrs["gen_ai.usage.output_tokens"]
    if tokenizer is not None:
        try:
            decoded = tokenizer.batch_decode(generated, skip_special_tokens=True)
            attrs.update(message_attributes([{"role": "assistant", "content": item[:MAX_TEXT]}
                                             for item in decoded[:MAX_ITEMS] if type(item) is str], "output"))
            if ids is not None:
                decoded_input = tokenizer.batch_decode(ids, skip_special_tokens=True)
                attrs.update(message_attributes([{"role": "user", "content": item[:MAX_TEXT]}
                                                 for item in decoded_input[:MAX_ITEMS] if type(item) is str], "input"))
        except Exception:
            # A tokenizer failure must not discard trustworthy ID-based usage.
            pass
    return attrs


def embedding_attributes(result: Any) -> dict:
    if type(result).__module__.split(".")[0] in ("torch", "numpy"):
        shape = result.shape
        return {"gen_ai.embeddings.dimension.count": int(shape[-1])} if len(shape) else {}
    for _ in range(4):
        if type(result) is not list or not result:
            break
        if type(result[0]) in (int, float):
            return {"gen_ai.embeddings.dimension.count": len(result)}
        result = result[0]
    return {}


def audio_attributes(source: Any, instance: Any, result: Any) -> dict:
    duration = None
    if type(source) is dict:
        rate = source.get("sampling_rate")
        samples = source.get("raw", source.get("array"))
    else:
        rate = getattr(getattr(instance, "feature_extractor", None), "sampling_rate", None)
        samples = source
    if type(rate) in (int, float) and rate > 0 and math.isfinite(rate):
        if type(samples).__module__.split(".")[0] in ("numpy", "torch") and len(samples.shape) == 1:
            duration = len(samples) / rate
        elif type(samples) in (list, tuple):
            duration = len(samples) / rate
    attrs = {"gen_ai.audio.duration_secs": duration} if duration is not None else {}
    records = result if type(result) is list else [result]
    transcripts = [text(item.get("text")) for item in records[:MAX_ITEMS] if type(item) is dict]
    transcripts = [item for item in transcripts if item is not None]
    if transcripts:
        transcript = "\n".join(transcripts)[:MAX_TEXT]
        attrs.update({"gen_ai.audio.transcript": transcript, "output.value": transcript, "output.mime_type": "text/plain"})
    return attrs
