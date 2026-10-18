.DEFAULT_GOAL := help
.NOTPARALLEL:
MAKEFLAGS     += --no-print-directory

# -- Colors ---------------------------------------------------
BOLD   := \033[1m
GREEN  := \033[32m
CYAN   := \033[36m
RED    := \033[31m
YELLOW := \033[33m
RESET  := \033[0m

# -- VM & Cluster Config --------------------------------------
# Scoped to this Makefile's commands (docker, kind) so the user's own Docker context is never switched.
# Hard-set rather than ?= so a DOCKER_CONTEXT exported in the shell cannot silently send kind to another daemon.
export DOCKER_CONTEXT := colima
CLUSTER ?= llm-lab
CPUS    ?= 8
MEM_GB  ?= 24
DISK_GB ?= 60

# -- Chart Versions & Namespaces ------------------------------
EG_VERSION   ?= v1.9.2
AIGW_VERSION ?= v1.2.0
NS           ?= llm-test
EG_NS        ?= envoy-gateway-system
AIGW_NS      ?= envoy-ai-gateway-system
MON_NS       ?= monitoring

# -- Gateways & Ports -----------------------------------------
GW           ?= eg
AI_GW        ?= eg-ai
GW_PORT      ?= 8080
AI_PORT      ?= 8081
PROM_PORT    ?= 9090
GRAFANA_PORT ?= 3000
BASE_URL      = http://localhost:$(GW_PORT)/v1/chat/completions
AI_URL        = http://localhost:$(AI_PORT)/v1/chat/completions

# -- Traffic & Load -------------------------------------------
NEW       ?= 0
ARGS      ?= -n 200 -c 20
RAMP_ARGS ?=
# Rule indexes patched by `make weight` and `make ai-weight`; see the comments in the route manifests
DEFAULT_RULE ?= 3
AI_RULE      ?= 0

# -- Host vllm-metal ------------------------------------------
HOST_PORT     ?= 8001
MODEL         ?= mlx-community/Llama-3.2-1B-Instruct-4bit
MEM_FRACTION  ?= 0.4
VENV          ?= $(HOME)/.venv-vllm-metal
INSTALL_ARGS  ?= --stable
HOST_IP       ?= $(shell colima ssh -- getent hosts host.lima.internal 2>/dev/null | awk '{print $$1}')

# -- Teardown bookkeeping -------------------------------------
# Setup records here what it created, so teardown only undoes our own changes
# and leaves anything that was already on the machine (docker, uv, Colima...) alone.
STATE      ?= .state
BREW_TOOLS := colima docker kind kubectl helm uv
KCTX        = kind-$(CLUSTER)
YES        ?=

# -- Helpers --------------------------------------------------
K = kubectl --context $(KCTX)
in_cluster = kind get clusters 2>/dev/null | grep -qx '$(CLUSTER)'
helm_rm = if helm --kube-context $(KCTX) status $(1) -n $(2) >/dev/null 2>&1; then helm --kube-context $(KCTX) uninstall $(1) -n $(2) --wait; else echo "$(1) not installed, skipping"; fi
rm_crds = crds=$$($(K) get crd -o name | grep -E '$(1)'); if [ -n "$$crds" ]; then $(K) delete $$crds --ignore-not-found; fi
svc =$$(kubectl -n $(EG_NS) get svc -l gateway.envoyproxy.io/owning-gateway-name=$(1),gateway.envoyproxy.io/owning-gateway-namespace=$(NS) -o name)
payload = {"model":"$(1)","messages":[{"role":"user","content":"hi"}],"max_tokens":8}
probe = printf '%-50s ' '$(1)'; curl -s -o /dev/null -D - -H 'Content-Type: application/json' -H '$(2)' -d '$(call payload,$(3))' $(4) | tr -d '\r' | awk 'NR==1{printf "%s  ", $$2} tolower($$1)=="x-served-by:"{printf "%s ", $$2} tolower($$1)=="x-inference-pod:"{printf "(%s)", $$2}'; echo
weight_patch = kubectl -n $(NS) patch $(1) $(2) --type=json -p '[{"op":"replace","path":"/spec/rules/$(3)/backendRefs/0/weight","value":$(shell expr 100 - $(NEW))},{"op":"replace","path":"/spec/rules/$(3)/backendRefs/1/weight","value":$(NEW)}]'
check_pct = test $(NEW) -ge 0 -a $(NEW) -le 100 || { printf "$(RED)NEW must be between 0 and 100$(RESET)\n" >&2; exit 1; }
host_manifest = sed -e 's/__HOST_IP__/$(HOST_IP)/g' -e 's/__HOST_PORT__/$(HOST_PORT)/g' real/host-backend.yaml.tpl
need_host_ip = test -n "$(HOST_IP)" || { printf "$(RED)HOST_IP not found, pass HOST_IP=<this Mac as seen from the VM>$(RESET)\n" >&2; exit 1; }

# -- Help -----------------------------------------------------
.PHONY: help
help: ## Show this help message
	@awk 'BEGIN {FS = ":.*##"; printf "Usage: make \033[36m<target>\033[0m\n"} \
	  /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0,5) } \
	  /^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2 }' \
	  $(MAKEFILE_LIST)

##@ Setup

.PHONY: install
install: ## Enable git hooks and sync Python deps
	@mkdir -p $(STATE)
	@test -f $(STATE)/hooks-path || git config --local --get core.hooksPath > $(STATE)/hooks-path || true
	git config core.hooksPath .githooks
	chmod +x .githooks/commit-msg
	uv sync --extra dev

.PHONY: tools
tools: ## Install missing host tooling with Homebrew (existing installs are left alone)
	@mkdir -p $(STATE)
	@for t in $(BREW_TOOLS); do \
	  if command -v $$t >/dev/null 2>&1; then echo "$$t already installed, leaving it alone"; \
	  else brew install $$t && { grep -qx $$t $(STATE)/brew-installed 2>/dev/null || echo $$t >> $(STATE)/brew-installed; }; fi; \
	done

.PHONY: vm
vm: ## Start the Colima VM that kind runs in (an already-running VM is left as is)
	@mkdir -p $(STATE)
	@if colima status >/dev/null 2>&1; then echo "Colima already running, not touching it"; \
	else \
	  colima list 2>/dev/null | awk 'NR>1 && $$1=="default"' | grep -q . || touch $(STATE)/colima-created; \
	  touch $(STATE)/colima-started; \
	  colima start --cpu $(CPUS) --memory $(MEM_GB) --disk $(DISK_GB) --vm-type vz; \
	fi

.PHONY: vm-stop
vm-stop: ## Stop the Colima VM
	colima stop

##@ Cluster

.PHONY: cluster
cluster: ## Create the kind cluster (idempotent)
	kind get clusters | grep -qx $(CLUSTER) || kind create cluster --name $(CLUSTER) --config kind/cluster.yaml
	kubectl version | grep -i server

.PHONY: eg
eg: ## Install Envoy Gateway with the AI Gateway extension hooks
	helm upgrade --install eg oci://docker.io/envoyproxy/gateway-helm --version $(EG_VERSION) -n $(EG_NS) --create-namespace \
	  -f helm/envoy-gateway-values.yaml --wait

.PHONY: ai
ai: ## Install the AI Gateway (Agent Router) CRDs and controller
	helm upgrade --install aieg-crd oci://docker.io/envoyproxy/ai-gateway-crds-helm --version $(AIGW_VERSION) -n $(AIGW_NS) \
	  --create-namespace
	helm upgrade --install aieg oci://docker.io/envoyproxy/ai-gateway-helm --version $(AIGW_VERSION) -n $(AIGW_NS) \
	  --create-namespace --wait

.PHONY: monitoring
monitoring: ## Install Prometheus and Grafana
	@mkdir -p $(STATE)
	@helm repo list 2>/dev/null | awk 'NR>1{print $$1}' | grep -qx prometheus-community \
	  || { helm repo add prometheus-community https://prometheus-community.github.io/helm-charts && touch $(STATE)/helm-repo-prometheus; }
	helm repo update prometheus-community
	helm upgrade --install kps prometheus-community/kube-prometheus-stack -n $(MON_NS) --create-namespace \
	  -f helm/kube-prometheus-stack-values.yaml --wait --timeout 10m

.PHONY: apply
apply: ## Apply gateways, backends, routes, monitors and dashboard
	kubectl apply -f manifests/
	kubectl -n $(NS) wait --for=condition=Available deployment --all --timeout=5m
	kubectl -n $(NS) wait --for=condition=Programmed gateway/$(GW) gateway/$(AI_GW) --timeout=5m

.PHONY: up
up: cluster eg ai monitoring apply ## Build everything from an empty Docker context

.PHONY: reset
reset: ## Put both routes back to their manifest state (weights 100/0, no mirror)
	kubectl replace -f manifests/30-route.yaml -f manifests/55-ai-route.yaml

.PHONY: down
down: ## Delete the kind cluster
	kind delete cluster --name $(CLUSTER)

##@ Inspect

.PHONY: forward
forward: ## Port-forward gateways :8080 :8081, Prometheus :9090, Grafana :3000 (admin/admin)
	@trap 'kill 0' EXIT INT TERM; \
	kubectl -n $(EG_NS) port-forward $(call svc,$(GW)) $(GW_PORT):80 & \
	kubectl -n $(EG_NS) port-forward $(call svc,$(AI_GW)) $(AI_PORT):80 & \
	kubectl -n $(MON_NS) port-forward svc/kps-kube-prometheus-stack-prometheus $(PROM_PORT):9090 & \
	kubectl -n $(MON_NS) port-forward svc/kps-grafana $(GRAFANA_PORT):80 & \
	wait

.PHONY: status
status: ## Show gateways, routes, backends and pods
	kubectl -n $(NS) get gateways.gateway.networking.k8s.io,httproutes.gateway.networking.k8s.io,aigatewayroutes.aigateway.envoyproxy.io,aiservicebackends.aigateway.envoyproxy.io,backends.gateway.envoyproxy.io
	kubectl -n $(NS) get pods -o wide
	kubectl -n $(EG_NS) get pods
	kubectl -n $(AIGW_NS) get pods

.PHONY: smoke
smoke: ## Plain gateway: which backend answers for each routing header
	@$(call probe,no header,x-none: 1,test-model,$(BASE_URL)); \
	$(call probe,x-llm-canary: true,x-llm-canary: true,test-model,$(BASE_URL)); \
	$(call probe,x-llm-canary: canary-model-test-configuration-92,x-llm-canary: canary-model-test-configuration-92,test-model,$(BASE_URL)); \
	$(call probe,x-llm-canary: flaky,x-llm-canary: flaky,test-model,$(BASE_URL))

.PHONY: ai-smoke
ai-smoke: ## AI gateway: model-name routing, the canary header and /v1/models
	@$(call probe,model test-model,x-none: 1,test-model,$(AI_URL)); \
	$(call probe,model test-model + x-llm-canary: true,x-llm-canary: true,test-model,$(AI_URL)); \
	$(call probe,model real-model (needs make real),x-none: 1,real-model,$(AI_URL)); \
	$(call probe,model unknown-model,x-none: 1,unknown-model,$(AI_URL)); \
	curl -s http://localhost:$(AI_PORT)/v1/models; echo

##@ Traffic

.PHONY: weight
weight: ## Plain gateway: send NEW percent of default traffic to llm-new: make weight NEW=10
	@$(check_pct)
	$(call weight_patch,httproutes.gateway.networking.k8s.io,llm,$(DEFAULT_RULE))

.PHONY: ai-weight
ai-weight: ## AI gateway: send NEW percent of test-model traffic to llm-new
	@$(check_pct)
	$(call weight_patch,aigatewayroutes.aigateway.envoyproxy.io,ai-llm,$(AI_RULE))

.PHONY: mirror-on
mirror-on: ## Plain gateway: mirror default traffic to llm-new (clients still get the weighted backend)
	kubectl -n $(NS) patch httproutes.gateway.networking.k8s.io llm --type=json -p '[{"op":"add","path":"/spec/rules/$(DEFAULT_RULE)/filters","value":[{"type":"RequestMirror","requestMirror":{"backendRef":{"name":"llm-new","port":8000}}}]}]'

.PHONY: mirror-off
mirror-off: ## Plain gateway: stop mirroring
	kubectl -n $(NS) patch httproutes.gateway.networking.k8s.io llm --type=json -p '[{"op":"remove","path":"/spec/rules/$(DEFAULT_RULE)/filters"}]'

##@ Load

.PHONY: load
load: ## Plain gateway load: make load ARGS="--stream -n 400 -H 'x-llm-canary: true'"
	uv run scripts/loadgen.py --url $(BASE_URL) $(ARGS)

.PHONY: ai-load
ai-load: ## AI gateway load: make ai-load ARGS="--model real-model -n 20 -c 4"
	uv run scripts/loadgen.py --url $(AI_URL) $(ARGS)

.PHONY: ramp
ramp: ## Plain gateway automated ramp with gates: make ramp RAMP_ARGS="--max-p95-ratio 2"
	uv run scripts/ramp.py --url $(BASE_URL) $(RAMP_ARGS)

.PHONY: ai-ramp
ai-ramp: ## AI gateway automated ramp with gates
	uv run scripts/ramp.py --url $(AI_URL) --make-target ai-weight $(RAMP_ARGS)

##@ Host Model

.PHONY: host-install
host-install: ## Install vllm-metal on this Mac (stable channel; INSTALL_ARGS= for the dev channel)
	curl -fsSL https://raw.githubusercontent.com/vllm-project/vllm-metal/main/install.sh | bash -s -- $(INSTALL_ARGS)

.PHONY: host-serve
host-serve: ## Run vllm-metal on the host, foreground: make host-serve MODEL=mlx-community/<model>
	. $(VENV)/bin/activate && VLLM_METAL_MEMORY_FRACTION=$(MEM_FRACTION) vllm serve $(MODEL) --host 0.0.0.0 --port $(HOST_PORT) --served-model-name test-model real-model

.PHONY: host-check
host-check: ## Check vllm-metal answers on the host
	curl -sS -m 5 http://localhost:$(HOST_PORT)/v1/models

.PHONY: real
real: ## Wire the cluster to vllm-metal on the host (HOST_IP found via Colima)
	@$(need_host_ip)
	$(host_manifest) | kubectl apply -f -

.PHONY: real-check
real-check: ## Check a pod in the cluster can reach vllm-metal on the host
	@$(need_host_ip)
	kubectl -n $(NS) run real-check --rm -i --restart=Never --image=curlimages/curl --command -- curl -sS -m 5 http://$(HOST_IP):$(HOST_PORT)/v1/models

.PHONY: real-smoke
real-smoke: ## Plain gateway: x-llm-canary: real reaches vllm-metal
	@$(call probe,x-llm-canary: real,x-llm-canary: real,real-model,$(BASE_URL))

.PHONY: real-as-new
real-as-new: ## Plain gateway: make vllm-metal the llm-new side of the weighted rule (make reset undoes it)
	kubectl -n $(NS) patch httproutes.gateway.networking.k8s.io llm --type=json -p '[{"op":"replace","path":"/spec/rules/$(DEFAULT_RULE)/backendRefs/1","value":{"group":"gateway.envoyproxy.io","kind":"Backend","name":"be-llm-real","weight":0,"filters":[{"type":"ResponseHeaderModifier","responseHeaderModifier":{"set":[{"name":"x-served-by","value":"llm-new"}]}}]}}]'

.PHONY: real-down
real-down: ## Remove the vllm-metal routes from the cluster
	@$(need_host_ip)
	$(host_manifest) | kubectl delete -f - --ignore-not-found

##@ Teardown

.PHONY: apply-down
apply-down: ## Undo `apply`: remove gateways, backends, routes, monitors and dashboard
	@if $(in_cluster); then $(K) delete -f manifests/ --ignore-not-found; else echo "no $(CLUSTER) cluster, skipping"; fi

.PHONY: monitoring-down
monitoring-down: ## Undo `monitoring`: remove Prometheus and Grafana (and the helm repo only if we added it)
	@if $(in_cluster); then \
	  $(call helm_rm,kps,$(MON_NS)); \
	  $(call rm_crds,monitoring\.coreos\.com); \
	  $(K) delete ns $(MON_NS) --ignore-not-found; \
	else echo "no $(CLUSTER) cluster, skipping"; fi
	@if [ -f $(STATE)/helm-repo-prometheus ]; then helm repo remove prometheus-community && rm -f $(STATE)/helm-repo-prometheus; \
	else echo "helm repo prometheus-community not added by us, keeping it"; fi

.PHONY: ai-down
ai-down: ## Undo `ai`: remove the AI Gateway controller and CRDs
	@if $(in_cluster); then \
	  $(call helm_rm,aieg,$(AIGW_NS)); \
	  $(call helm_rm,aieg-crd,$(AIGW_NS)); \
	  $(call rm_crds,aigateway\.envoyproxy\.io); \
	  $(K) delete ns $(AIGW_NS) --ignore-not-found; \
	else echo "no $(CLUSTER) cluster, skipping"; fi

.PHONY: eg-down
eg-down: ## Undo `eg`: remove Envoy Gateway and its CRDs
	@if $(in_cluster); then \
	  $(call helm_rm,eg,$(EG_NS)); \
	  $(call rm_crds,envoyproxy\.io|gateway\.networking\.(x-)?k8s\.io); \
	  $(K) delete ns $(EG_NS) --ignore-not-found; \
	else echo "no $(CLUSTER) cluster, skipping"; fi

.PHONY: teardown
teardown: apply-down monitoring-down ai-down eg-down ## Undo `up` inside the cluster, keeping the kind cluster itself

.PHONY: vm-delete
vm-delete: ## Undo `vm`: stop/delete Colima only if we started/created it
	@if kind get clusters 2>/dev/null | grep -qvx '$(CLUSTER)'; then \
	  printf "$(RED)other kind clusters exist, leaving Colima alone$(RESET)\n" >&2; exit 1; fi
	@if [ -f $(STATE)/colima-created ]; then colima delete --force && rm -f $(STATE)/colima-created $(STATE)/colima-started; \
	elif [ -f $(STATE)/colima-started ]; then colima stop && rm -f $(STATE)/colima-started; \
	else echo "Colima was not started by us, leaving it alone"; fi

.PHONY: host-uninstall
host-uninstall: ## Undo host-install: remove the vllm-metal venv (never uv, never the model cache)
	@if pgrep -f '[v]llm serve' >/dev/null; then printf "$(RED)vllm serve is running, stop it first$(RESET)\n" >&2; exit 1; fi
	@if [ -f "$(VENV)/pyvenv.cfg" ]; then rm -rf "$(VENV)" && echo "removed $(VENV)"; else echo "$(VENV) is not a venv, leaving it"; fi

.PHONY: model-rm
model-rm: ## Delete the cached HuggingFace weights for MODEL (opt-in, the cache is shared; not in nuke)
	@test -n "$(MODEL)"
	rm -rf "$${HF_HOME:-$(HOME)/.cache/huggingface}/hub/models--$(subst /,--,$(MODEL))"

.PHONY: uninstall
uninstall: ## Undo `install`: restore git hooks path and remove the project .venv (uv itself stays)
	@cur=$$(git config --local --get core.hooksPath || true); \
	if [ "$$cur" = .githooks ]; then \
	  prev=$$(cat $(STATE)/hooks-path 2>/dev/null); \
	  if [ -n "$$prev" ]; then git config --local core.hooksPath "$$prev"; else git config --local --unset core.hooksPath; fi; \
	  rm -f $(STATE)/hooks-path; \
	else echo "core.hooksPath is '$$cur', not ours, leaving it"; fi
	@if [ -f .venv/pyvenv.cfg ]; then rm -rf .venv && echo "removed .venv"; fi

.PHONY: tools-remove
tools-remove: ## Undo `tools`: brew-uninstall only what it installed (skips anything other formulae need)
	@test -s $(STATE)/brew-installed || { echo "nothing recorded as installed by make tools"; exit 0; }
	@for t in $$(cat $(STATE)/brew-installed); do \
	  deps=$$(brew uses --installed $$t 2>/dev/null | grep -vxFf $(STATE)/brew-installed | tr '\n' ' '); \
	  if [ -n "$$deps" ]; then echo "keeping $$t, needed by: $$deps"; continue; fi; \
	  brew uninstall --ignore-dependencies $$t && sed -i '' "/^$$t$$/d" $(STATE)/brew-installed; \
	done

.PHONY: confirm
confirm:
	@test "$(YES)" = 1 || { printf "$(YELLOW)Tear down cluster, Colima (if ours), host venv, .venv and brew tools we installed? [y/N] $(RESET)"; read -r a; test "$$a" = y; }

.PHONY: nuke
nuke: confirm down vm-delete host-uninstall uninstall tools-remove ## Undo all setup (asks first, YES=1 skips); pre-existing tools are kept
	@rmdir $(STATE) 2>/dev/null || true

##@ Test

.PHONY: test
test: ## Run the Python tests
	uv run pytest
