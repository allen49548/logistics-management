.PHONY: install test demo check-privacy clean package

install:
	@bash install.sh

test:
	python3 -m unittest discover -s tests -v

demo:
	python3 scripts/payroll.py --profile example demo --month 2026-09

# 提交/开源前自检：可开源文件中不得混入真实路径、真实会话 ID 或真实 open_id
check-privacy:
	@echo "扫描可开源文件..."
	@if grep -RInE 'Desktop/WPS|冷链物流|oc_[0-9a-f]{20}|ou_[0-9a-f]{20}' \
		--exclude=Makefile --exclude=install.sh \
		--exclude-dir=.git --exclude-dir=local --exclude-dir=out \
		--exclude-dir=state --exclude-dir=dist --exclude-dir=__pycache__ . ; then \
		echo "FAIL: 发现疑似真实数据，清理后再提交"; exit 1; \
	else \
		echo "OK: 未发现真实数据"; \
	fi
	@if [ -f config/local/payroll.json ]; then \
		echo "提示: config/local/ 已被 .gitignore 排除，不会被提交"; \
	fi

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf examples/out/*/ .pytest_cache

package: check-privacy test
	@mkdir -p dist
	@zip -rq dist/driver-wage.zip . \
		-x '.git/*' 'config/local/*' 'out/*' 'examples/out/*' 'state/*' \
		   'dist/*' '__pycache__/*' '*/.DS_Store' '*.pyc'
	@echo "已生成 dist/driver-wage.zip（不含任何真实数据）"
