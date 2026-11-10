"""Stage revision-pinned upstream ONNX test fixtures BEFORE network isolation.

Weights stay outside the package/repository and are never redistributed by traceAI.
These tiny random models are for contracts, not useful inference. Upstream model
cards do not declare a license; the manifest records that rather than inventing one.
"""
import hashlib
import json
import pathlib
import sys
import urllib.request

MODELS = {
    "llama": ("onnx-internal-testing/tiny-random-LlamaForCausalLM-ONNX", "344428622118ee8b46c8702679173bf6bdbd6497"),
    "t5": ("hf-internal-testing/tiny-random-T5ForConditionalGeneration", "b12e41904f9f5c33909e8f6a21cf638eca966e74"),
    "bert": ("onnx-internal-testing/tiny-random-BertModel-ONNX", "3f74b196fbb9932eddd11953553cb7ce04ae671a"),
    "qa": ("onnx-internal-testing/tiny-random-BertForQuestionAnswering-ONNX", "79c27929ad1c20e7c5c7a60ccbe64b1e64cfa309"),
}
FILES = {"config.json", "tokenizer.json", "tokenizer_config.json", "generation_config.json", "special_tokens_map.json", "onnx/model.onnx", "onnx/encoder_model.onnx", "onnx/decoder_model_merged.onnx"}


def main():
    root = pathlib.Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    manifest = {}
    for alias, (repo, revision) in MODELS.items():
        url = f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true"
        with urllib.request.urlopen(url, timeout=120) as response:
            metadata = json.load(response)
        assert metadata["sha"] == revision
        entry = {"repo": repo, "revision": revision, "license": metadata.get("cardData", {}).get("license", "not declared by upstream"), "files": {}}
        for item in metadata["siblings"]:
            name = item["rfilename"]
            if name not in FILES:
                continue
            destination = root / alias / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(f"https://huggingface.co/{repo}/resolve/{revision}/{name}", timeout=180) as response:
                data = response.read()
            digest = hashlib.sha256(data).hexdigest()
            if "lfs" in item:
                assert digest == item["lfs"]["sha256"], name
            else:
                blob = b"blob " + str(len(data)).encode() + b"\0" + data
                assert hashlib.sha1(blob).hexdigest() == item["blobId"], name
            destination.write_bytes(data)
            entry["files"][name] = {"sha256": digest, "bytes": len(data)}
        manifest[alias] = entry
        print(alias, sum(x["bytes"] for x in entry["files"].values()), "bytes", flush=True)
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
