# Environment Variables

Complete reference for all traceAI environment variables.

## Authentication

| Variable | Required | Description |
|----------|----------|-------------|
| `FI_API_KEY` | Yes | API key for Future AGI authentication |
| `FI_SECRET_KEY` | Yes | Secret key for Future AGI authentication |

```bash
export FI_API_KEY="your-api-key"
export FI_SECRET_KEY="your-secret-key"
```

## Collector Endpoints

| Variable | Default | Description |
|----------|---------|-------------|
| `FI_BASE_URL` | `https://api.futureagi.com` | HTTP collector endpoint |
| `FI_GRPC_URL` | `https://grpc.futureagi.com` | gRPC collector endpoint |
| `FI_COLLECTOR_ENDPOINT` | - | Full collector URL (overrides FI_BASE_URL) |
| `FI_GRPC_COLLECTOR_ENDPOINT` | - | Full gRPC collector URL |

```bash
# Use custom endpoint
export FI_BASE_URL="https://your-collector.com"

# Or specify full endpoint
export FI_COLLECTOR_ENDPOINT="https://your-collector.com/v1/traces"
```

## Project Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `FI_PROJECT_NAME` | `DEFAULT_PROJECT_NAME` | Project identifier |
| `FI_PROJECT_VERSION_NAME` | `DEFAULT_PROJECT_VERSION_NAME` | Version identifier |

```bash
export FI_PROJECT_NAME="my-ai-app"
export FI_PROJECT_VERSION_NAME="v1.2.0"
```

## Privacy Controls (TraceConfig)

| Variable | Default | Description |
|----------|---------|-------------|
| `FI_HIDE_INPUTS` | `false` | Hide all input values |
| `FI_HIDE_OUTPUTS` | `false` | Hide all output values |
| `FI_HIDE_INPUT_MESSAGES` | `false` | Hide input message content |
| `FI_HIDE_OUTPUT_MESSAGES` | `false` | Hide output message content |
| `FI_HIDE_INPUT_IMAGES` | `false` | Hide base64 images in inputs |
| `FI_HIDE_INPUT_TEXT` | `false` | Hide text in input messages |
| `FI_HIDE_OUTPUT_TEXT` | `false` | Hide text in output messages |
| `FI_HIDE_EMBEDDING_VECTORS` | `false` | Hide embedding vectors |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | `false` | Hide model parameters |
| `FI_BASE64_IMAGE_MAX_LENGTH` | `32000` | Max chars for base64 images |

```bash
# Production privacy settings
export FI_HIDE_INPUTS=true
export FI_HIDE_OUTPUTS=true
export FI_HIDE_EMBEDDING_VECTORS=true
```

## OpenTelemetry Batch Processor

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_BSP_SCHEDULE_DELAY` | `5000` | Batch export delay in milliseconds |
| `OTEL_BSP_MAX_QUEUE_SIZE` | `2048` | Maximum queue size |
| `OTEL_BSP_MAX_EXPORT_BATCH_SIZE` | `512` | Maximum batch size |
| `OTEL_BSP_EXPORT_TIMEOUT` | `30000` | Export timeout in milliseconds |

```bash
# High-throughput settings
export OTEL_BSP_SCHEDULE_DELAY=1000
export OTEL_BSP_MAX_QUEUE_SIZE=4096
export OTEL_BSP_MAX_EXPORT_BATCH_SIZE=1024
```

## Debugging

| Variable | Default | Description |
|----------|---------|-------------|
| `FI_VERBOSE_EXPORTER` | `false` | Enable verbose exporter logging |
| `FI_VERBOSE_PROVIDER` | `false` | Enable verbose provider logging |

```bash
# Enable debug logging
export FI_VERBOSE_EXPORTER=true
export FI_VERBOSE_PROVIDER=true
```

## Performance Tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `FI_MAX_ACTIVE_SPANS_TRACKED` | `100` | Max active spans tracked |

```bash
# Increase for high-concurrency apps
export FI_MAX_ACTIVE_SPANS_TRACKED=500
```

## Example Configurations

### Development

```bash
# .env.development
FI_API_KEY=dev-api-key
FI_SECRET_KEY=dev-secret-key
FI_PROJECT_NAME=my-app-dev
FI_VERBOSE_EXPORTER=true
```

### Production

```bash
# .env.production
FI_API_KEY=prod-api-key
FI_SECRET_KEY=prod-secret-key
FI_PROJECT_NAME=my-app
FI_HIDE_INPUTS=true
FI_HIDE_OUTPUTS=true
OTEL_BSP_MAX_QUEUE_SIZE=4096
```

### Testing

```bash
# .env.test
FI_API_KEY=test-api-key
FI_SECRET_KEY=test-secret-key
FI_PROJECT_NAME=my-app-test
FI_BASE_URL=http://localhost:4318
```

## Loading Environment Variables

### Python (python-dotenv)

```python
from dotenv import load_dotenv
load_dotenv()

from fi_instrumentation import register
# Variables are automatically read
```

### TypeScript (dotenv)

```typescript
import * as dotenv from "dotenv";
dotenv.config();

import { register } from "@traceai/fi-core";
// Variables are automatically read
```

### Docker

```yaml
# docker-compose.yml
services:
  app:
    environment:
      - FI_API_KEY=${FI_API_KEY}
      - FI_SECRET_KEY=${FI_SECRET_KEY}
      - FI_PROJECT_NAME=my-app
```

### Kubernetes

```yaml
# deployment.yaml
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
        - name: app
          env:
            - name: FI_API_KEY
              valueFrom:
                secretKeyRef:
                  name: traceai-secrets
                  key: api-key
            - name: FI_SECRET_KEY
              valueFrom:
                secretKeyRef:
                  name: traceai-secrets
                  key: secret-key
```

## Related

- [TraceConfig](trace-config.md) - Privacy settings in detail
- [Installation](../getting-started/installation.md) - Setup guide
