# Routes to vllm-metal running on the Mac host. __HOST_IP__ and __HOST_PORT__ are filled in by `make real`.
# Metal cannot be reached from inside a Linux VM, so this one backend lives outside the cluster.
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: Backend
metadata: {name: be-llm-real, namespace: llm-test}
spec:
  endpoints:
  - ip: {address: __HOST_IP__, port: __HOST_PORT__}
---
# Plain gateway: header x-llm-canary: real. A separate HTTPRoute avoids shifting the default rule index.
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: {name: llm-real, namespace: llm-test}
spec:
  parentRefs: [{name: eg}]
  rules:
  - matches:
    - path: {type: PathPrefix, value: /v1}
      headers:
      - {name: x-llm-canary, value: real}
    filters:
    - type: ResponseHeaderModifier
      responseHeaderModifier:
        set: [{name: x-served-by, value: llm-real}]
    backendRefs:
    - {group: gateway.envoyproxy.io, kind: Backend, name: be-llm-real}
---
# AI gateway: model name real-model. `make host-serve` serves both test-model and real-model, so the real backend
# can also stand in for llm-new on the test-model routes
apiVersion: aigateway.envoyproxy.io/v1beta1
kind: AIServiceBackend
metadata: {name: ai-llm-real, namespace: llm-test}
spec:
  schema: {name: OpenAI}
  backendRef: {group: gateway.envoyproxy.io, kind: Backend, name: be-llm-real}
---
apiVersion: aigateway.envoyproxy.io/v1beta1
kind: AIGatewayRoute
metadata: {name: ai-llm-real, namespace: llm-test}
spec:
  parentRefs:
  - {name: eg-ai, kind: Gateway, group: gateway.networking.k8s.io}
  rules:
  - matches:
    - headers:
      - {type: Exact, name: x-ai-eg-model, value: real-model}
    backendRefs:
    - {name: ai-llm-real}
---
# Prometheus scrapes the host process directly; vLLM publishes /metrics on its API port
apiVersion: monitoring.coreos.com/v1alpha1
kind: ScrapeConfig
metadata: {name: llm-real, namespace: llm-test}
spec:
  metricsPath: /metrics
  staticConfigs:
  - targets: ["__HOST_IP__:__HOST_PORT__"]
    labels: {app: llm-real}
