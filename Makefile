.PHONY: release test dev

# ============ 发布与部署 ============
release: ## 发布版本（自动递增版本号并推送标签，触发 GitHub Action 构建镜像）
ifeq ($(OS),Windows_NT)
	@pwsh -NoProfile -ExecutionPolicy Bypass -File script/release.ps1
else
	@bash script/release.sh
endif

# ============ 开发与自测 ============
test: ## 运行回归自测
	@python -m unittest discover -s test -t . -v

dev: ## 本地开发启动（默认 http://127.0.0.1:3002）
	@python -m src.server
