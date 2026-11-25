# -*- coding: utf-8 -*-
# @Time    : 2026-11-13 10:17:13
# @Author  : yangyuexiong
# @File    : generate_knowledge_phase3_fixtures.py

"""生成第三阶段结构处理及检索样例，不连接业务数据库或调用供应商。"""

import io
import json
from pathlib import Path

HTML_FIXTURE = """<!doctype html><html lang="zh-CN"><head><title>制度知识</title>
<script>脚本机密标记不得进入索引</script><style>.hidden{display:none}</style></head>
<body><header>顶部广告标记</header><nav>导航噪声标记</nav><aside>侧栏营销标记</aside>
<main><article><header><h1 id="travel">差旅报销制度</h1></header>
<p>报销申请须在<strong>十五个工作日</strong>内提交。</p>
<h2 id="approval">审批与额度</h2><p>审批部门为财务部，住宿限额为五千元。</p>
<table><tr><th>供应商</th><th>单价</th><th>数量</th></tr>
<tr><td>甲供应商</td><td>3800</td><td>50</td></tr><tr><td>乙供应商</td><td>3900</td><td>30</td></tr></table>
<pre>if amount &lt; 5000:\n    approve = True</pre>
<p hidden>隐藏正文机密标记</p><p aria-hidden="true">读屏隐藏标记</p>
<p style="display:none">样式隐藏标记</p><script>正文脚本机密标记</script>
<img src="https://example.invalid/image.png"><a href="https://example.invalid/policy">参考制度</a>
</article></main><footer>页脚推广标记</footer></body></html>"""
CONFIGURATION_FIXTURE = {
    "service": {
        "name": "差旅服务",
        "approval_department": "财务部",
        "claim_deadline_workdays": 15,
        "hotel_limit_cny": 5000,
        "enabled": True,
        "endpoint": "${POLICY_ENDPOINT}",
    },
    "suppliers": [
        {
            "name": "甲供应商",
            "unit_cny": 3800,
            "quantity": 50,
        },
        {
            "name": "乙供应商",
            "unit_cny": 3900,
            "quantity": 30,
        },
    ],
    "a/b": {
        "~key": "路径转义样例",
    },
    "empty": {},
}
YAML_FIXTURE = """service:
  name: 差旅服务
  approval_department: 财务部
  claim_deadline_workdays: 15
  hotel_limit_cny: 5000
  enabled: true
  endpoint: ${POLICY_ENDPOINT}
  launch_date: 2026-11-13
  on: 保留字符串键
suppliers:
  - name: 甲供应商
    unit_cny: 3800
    quantity: 50
  - name: 乙供应商
    unit_cny: 3900
    quantity: 30
defaults: &defaults
  retry_count: 3
backup: *defaults
"""


def make_pptx_fixture() -> bytes:
    """用标准 Office 库生成页序重排、隐藏内容、表格和真实备注的演示文稿。"""
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    policy = presentation.slides.add_slide(presentation.slide_layouts[1])
    policy.shapes.title.text = "差旅报销制度"
    policy.placeholders[1].text = "审批部门为财务部。住宿费用上限为五千元。"
    policy.notes_slide.notes_text_frame.text = "讲解备注：报销凭证需保留原件。"
    hidden_shape = policy.shapes.add_textbox(Inches(1), Inches(5), Inches(3), Inches(1))
    hidden_shape.text = "隐藏对象专用标记"
    hidden_shape._element.xpath(".//p:cNvPr")[0].set("hidden", "1")
    table = policy.shapes.add_table(3, 3, Inches(1), Inches(3), Inches(7), Inches(1)).table
    for row, values in enumerate((("供应商", "单价", "数量"), ("甲供应商", "3800", "50"), ("乙供应商", "3900", "30"))):
        for column, value in enumerate(values):
            table.cell(row, column).text = value
    picture = io.BytesIO()
    Image.new("RGB", (30, 30), "white").save(picture, format="PNG")
    picture.seek(0)
    policy.shapes.add_picture(picture, Inches(8), Inches(3), Inches(0.3))
    hidden = presentation.slides.add_slide(presentation.slide_layouts[1])
    hidden.shapes.title.text = "隐藏页专用标记"
    hidden.placeholders[1].text = "此页不应进入知识"
    hidden._element.set("show", "0")
    deadlines = presentation.slides.add_slide(presentation.slide_layouts[1])
    deadlines.shapes.title.text = "提交期限"
    deadlines.placeholders[1].text = "报销申请须在十五个工作日内提交。"
    deadlines.notes_slide.notes_text_frame.text = "讲解备注：例外情况需主管确认。"
    paragraph = deadlines.placeholders[1].text_frame.paragraphs[0]
    paragraph.runs[0].hyperlink.address = "https://example.invalid/never-load"
    ids = presentation.slides._sldIdLst
    ids.insert(0, ids[-1])
    output = io.BytesIO()
    presentation.save(output)
    return output.getvalue()


def phase3_fixtures() -> dict[str, bytes]:
    """提供固定正常与异常样例，供页面测试和隔离自动化共用。"""
    return {
        "policy.html": HTML_FIXTURE.encode(),
        "policy.json": json.dumps(CONFIGURATION_FIXTURE, ensure_ascii=False, indent=2).encode(),
        "policy.yaml": YAML_FIXTURE.encode(),
        "policy.pptx": make_pptx_fixture(),
        "duplicate.json": b'{"limit":5000,"limit":9000}',
        "duplicate.yaml": b"limit: 5000\nlimit: 9000\n",
        "cycle.yaml": b"loop: &loop [*loop]\n",
        "merge.yaml": b"base: &base {limit: 5000}\npolicy: {<<: *base}\n",
        "unsafe-tag.yaml": b"value: !!python/object/apply:os.system ['echo not-executed']\n",
        "noise-only.html": b"<html><head><script>not knowledge</script></head><body><nav>menu</nav></body></html>",
    }


def main() -> None:
    """写入固定样例及操作步骤，人工测试结论应保存在独立记录中。"""
    root = Path(__file__).resolve().parents[1] / "docs" / "knowledge-phase3-manual"
    root.mkdir(parents=True, exist_ok=True)
    for name, content in phase3_fixtures().items():
        (root / name).write_bytes(content)
    (root / "README.md").write_text(
        """# M6.4 第三阶段页面测试

先更新依赖（生成 PPTX 样例需要开发依赖 python-pptx），API/worker 使用当前代码及迁移 head。
运行 `.venv/bin/python -m scripts.generate_knowledge_phase3_fixtures` 可重新生成本目录，不调用供应商。
建议新建关键词知识库，先选「保留原文」或未配置加工模型的「自动处理」。

1. 上传 policy.html。加工结果应有 HTML 主文来源、标题和表格行。搜索「十五个工作日」「财务部」「甲供应商」应命中；导航噪声、广告、隐藏内容和脚本标记不应命中。
2. 上传 policy.json。预览应保留 /service、/suppliers/0、/suppliers/1 等路径，字段类型和值分开；甲供应商的 3800/50 不应与乙供应商记录混合。
路径 /a~1b/~0key 应正确转义。
3. 上传 policy.yaml。同 JSON 一样保留逻辑路径并展示值定义行号；日期、on 键、${POLICY_ENDPOINT} 不执行或替换，非循环别名按不同路径展开。
4. 上传 policy.pptx。实际第 1 页标题为「提交期限」，第 2 页是「差旅报销制度」，第 3 页被隐藏并排除。备注和正文应分别展示；表格按行保存；图片不作语义识别。
5. 逐份上传 duplicate.json、duplicate.yaml、cycle.yaml、merge.yaml、unsafe-tag.yaml。任务应 invalid_structure 失败，旧索引仍可用，不能悄悄覆盖配置键或执行代码。
6. noise-only.html 应失败，不把菜单当知识。对重复上传、更新版本、重建、撤权和正常配置下删除分别复测。
7. 配置已验证文本加工模型后，新增格式的 auto 不产生加工调用；选 enhanced 后应展示独立模型解释及用量，原始证据和路径/页码不改变。
8. 手机宽度核对长配置路径、HTML 标题和幻灯片备注没有横向溢出；关闭抽屉和撤权后旧证据应被清除。

仅解析 UTF-8 HTML/JSON/YAML 与无宏 PPTX。YAML 合并键、自定义标签、循环别名、非字符串对象键及多文档输入明确拒绝。
PPTX 不执行外部链接、宏或嵌入对象，不识别图片、图表和复杂版面含义；没有提供 PPTX 原页图片预览。
JSON/YAML 是配置文件中的值，不代表部署后实际生效状态。配置数组对象不跨记录合并；超长单值/记录明确拒绝发布。
模型解释质量和实际检索收益需用业务样例复核，不能用自动化假模型代替。
""",
        encoding="utf-8",
    )
    print(root)


if __name__ == "__main__":
    main()
