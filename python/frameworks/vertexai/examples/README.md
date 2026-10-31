# Vertex AI examples

For new applications, use [`google_genai_vertex.py`](google_genai_vertex.py). It uses `google-genai` with `vertexai=True` and `traceAI-google-genai`. Follow the [package setup guide](../README.md#set-up-google-gen-ai-recommended) for credentials and environment variables, then run:

```bash
pip install traceAI-google-genai google-genai
python google_genai_vertex.py
```

## Legacy examples

The other scripts in this directory use the legacy `google-cloud-aiplatform` SDK. Its generative AI modules were deprecated and [scheduled for removal after June 24, 2026](https://cloud.google.com/vertex-ai/generative-ai/docs/deprecations/genai-vertexai-sdk). Install the supported legacy version with:

```bash
pip install -r requirements.txt
```

Start with [`basic_generation.py`](basic_generation.py), which reads the model ID from `GOOGLE_GENAI_MODEL` rather than choosing a potentially retired model. Older modality-specific scripts may name models or sample assets that are no longer available; select a supported model and your own assets before using them. The SDK pin preserves imports, not model availability. Use the [Google migration guide](https://cloud.google.com/vertex-ai/generative-ai/docs/deprecations/genai-vertexai-sdk) to move those applications to `google-genai`.
