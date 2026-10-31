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

Follow the [legacy setup guide](../README.md#keep-an-existing-legacy-application-running) for credentials and environment variables. Use a region where your model is available, such as `export GOOGLE_CLOUD_LOCATION="us-central1"`; older supported SDKs reject `global`, so override it if you set it for the recommended route.

Start with [`basic_generation.py`](basic_generation.py), which defaults to `us-central1` when the location is unset and reads the model ID from `GOOGLE_GENAI_MODEL` rather than choosing a potentially retired model. Older modality-specific scripts may name models or sample assets that are no longer available; select a supported model and your own assets before using them. The SDK pin preserves imports, not model availability. Use the [Google migration guide](https://cloud.google.com/vertex-ai/generative-ai/docs/deprecations/genai-vertexai-sdk) to move those applications to `google-genai`.
