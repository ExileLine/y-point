"""允许通过 python3 -m ypoint_cli 使用同一命令入口。"""

from ypoint_cli.main import main

raise SystemExit(main())
