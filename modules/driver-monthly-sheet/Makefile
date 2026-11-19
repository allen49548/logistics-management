.PHONY: install test selftest check-privacy clean

install:
	@bash install.sh

test: selftest
	python3 -m unittest discover -s tests -v

selftest:
	python3 scripts/driver_sheet.py selftest

# 提交前自检：可公开文件中不得混入真实路径 / 姓名 / 会话 ID
check-privacy:
	@echo "扫描可公开文件..."
	@if grep -RInE '/Users/liaohaocheng|Desktop/WPS|oc_[0-9a-f]{20}|ou_[0-9a-f]{20}|周小毛|贾燕斌|王博成|蒙祖友|杨海湘|辛荣超|劳辉厚|康国强' \
		--exclude-dir=.git --exclude-dir=local --exclude-dir=state \
		--exclude='*.pyc' --exclude-dir=__pycache__ --exclude=Makefile . ; then \
		echo "FAIL: 发现疑似真实数据，清理后再提交"; exit 1; \
	else echo "OK: 未发现真实数据"; fi

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache
