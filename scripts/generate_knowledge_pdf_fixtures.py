# -*- coding: utf-8 -*-
# @Time    : 2026-11-12 22:18:07
# @Author  : yangyuexiong
# @File    : generate_knowledge_pdf_fixtures.py

"""生成 PDF 第二阶段可重复手工验收样例，不连接业务库或调用模型。"""

import io
from pathlib import Path

from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas


def make_pdf_fixture(kind: str) -> bytes:
    """构造文字、扫描、混合、表格、双栏、空白或加密的真实 PDF。"""
    output = io.BytesIO()
    canvas = Canvas(output, pagesize=(600, 800))
    canvas.setTitle("Knowledge PDF verification")
    if kind == "chinese-scan":
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont

        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        canvas.setFont("STSong-Light", 22)
        canvas.drawString(48, 730, "差旅报销管理规定")
        canvas.drawString(48, 670, "报销申请须在十五个工作日内提交。")
        canvas.drawString(48, 620, "住宿费用上限为五千元。")
        canvas.drawString(48, 570, "审批部门为财务部。")
    elif kind in {"native", "mixed", "encrypted", "scan"}:
        canvas.setFont("Helvetica", 18)
        canvas.drawString(48, 730, "Travel reimbursement policy")
        canvas.setFont("Helvetica", 14)
        canvas.drawString(48, 680, "Submit claims within fifteen working days.")
        canvas.drawString(48, 650, "Hotel allowance: 5000 CNY per trip.")
        canvas.drawString(48, 620, "Approval owner: Finance Department.")
    elif kind == "table":
        for x in (48, 240, 420, 550):
            canvas.line(x, 500, x, 700)
        for y in (500, 550, 600, 650, 700):
            canvas.line(48, y, 550, y)
        for y, values in zip(
            (670, 620, 570, 520),
            (("Supplier", "Unit CNY", "Quantity"), ("Alpha", "3800", "50"), ("Beta", "3900", "30"), ("Gamma", "4000", "20")),
            strict=True,
        ):
            for x, text in zip((58, 250, 430), values, strict=True):
                canvas.drawString(x, y, text)
    elif kind == "columns":
        for row in range(4):
            canvas.drawString(48, 700 - row * 30, f"Left item {row + 1}")
            canvas.drawString(350, 700 - row * 30, f"Right item {row + 1}")
    elif kind not in {"blank", "unreadable"}:
        raise ValueError("未知测试样例")
    if kind == "unreadable":
        image = Image.new("RGB", (600, 800), "#ddd")
        canvas.drawImage(ImageReader(image), 0, 0, width=600, height=800)
        image.close()
    canvas.showPage()
    canvas.save()
    content = output.getvalue()
    if kind in {"scan", "mixed", "chinese-scan"}:
        from app.services.knowledge_pdf import render_pdf_page

        image = Image.open(io.BytesIO(render_pdf_page(content, 1)))
        scanned = io.BytesIO()
        target = Canvas(scanned, pagesize=(600, 800))
        target.drawImage(ImageReader(image), 0, 0, width=600, height=800)
        target.showPage()
        target.save()
        image.close()
        if kind in {"scan", "chinese-scan"}:
            return scanned.getvalue()
        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(content)))
        writer.append(PdfReader(io.BytesIO(scanned.getvalue())))
        writer.add_blank_page(width=600, height=800)
        combined = io.BytesIO()
        writer.write(combined)
        return combined.getvalue()
    if kind == "encrypted":
        writer = PdfWriter()
        writer.append(PdfReader(io.BytesIO(content)))
        writer.encrypt("fixture-password")
        encrypted = io.BytesIO()
        writer.write(encrypted)
        return encrypted.getvalue()
    return content


def main() -> None:
    """输出固定文件及操作说明，不覆盖人工测试记录。"""
    root = Path(__file__).resolve().parents[1] / "docs" / "knowledge-phase2-manual"
    root.mkdir(parents=True, exist_ok=True)
    for kind in ("native", "scan", "chinese-scan", "mixed", "table", "columns", "blank", "unreadable", "encrypted"):
        (root / f"{kind}.pdf").write_bytes(make_pdf_fixture(kind))
    (root / "README.md").write_text(
        """# PDF 第二阶段手工验收

服务端和 worker 须执行 `alembic upgrade head` 升级至当前代码版本（当前为 054），安装 Python 依赖及 Tesseract / chi_sim、eng 语言包。
样例共九份，可运行 `.venv/bin/python -m scripts.generate_knowledge_pdf_fixtures` 重新生成；不连接业务库或调用模型。
默认 OCR 语言为 chi_sim+eng、分辨率 180 DPI、最低置信度 60、单文件 OCR 页数上限 20。
生成脚本会更新本说明和固定样例；人工验收记录请另存文件。

新建关键词知识库，文档处理方式先选「保留原文」，避免真实模型费用。

1. 上传 native.pdf：自动提取，文字页、非 OCR；检索 Hotel，页码为 1，原页高亮覆盖证据。
2. 上传 scan.pdf：自动 OCR，识别结果包含 Hotel / 5000，显示置信度和布局待核对说明。
3. 上传 mixed.pdf：预览摘要为 3 页、OCR 1 页、空白 1 页；第 1/2 页分别为文字层/OCR，空白页不生成证据。
4. 上传 table.pdf：展示表格行和列，不把 3800 / 50 与其他供应商拼接；查看原页可核对区域。
5. 上传 columns.pdf：原文先为 Left item 1–4，再为 Right item 1–4，不左右逐行混读。
6. blank.pdf 没有有效内容，应失败；unreadable.pdf 没有可靠识别文字，应失败且保留旧索引。
7. encrypted.pdf 应解析失败；scan.pdf 选择「仅提取文字层」应提示需要 OCR。
8. 更新同一 native.pdf，仅改为「强制 OCR」：生成新版本，展示 OCR，不能错误去重复用旧解析。
9. 原页打开后关闭再打开；切到手机宽度；撤销共享或删除文档后重新读取原页，应拒绝访问并清除图片。
10. 上传 chinese-scan.pdf：自动 OCR，分别检索「五千元」「十五个工作日」「财务部」，均应命中；原文不应把这些词拆成空格分隔的汉字，原页可逐项核对。
11. 配置已验证文本加工模型，选择增强理解再上传 native.pdf：独立展示模型解释；不应改写原文数值。
12. 如需验证自动加工：配置文本加工模型，处理方式选自动，分别上传文字页、扫描页、表格；仅后两者产生模型加工调用。
13. 同一 scan.pdf 先自动提取成功，再更新为「仅提取文字层」：任务应失败且旧索引/原页仍可用；
再更新为「强制 OCR」应成功发布新版本。正常配置下删除文档后，旧引用和原页均须拒绝访问。

当前 PDF 解析版本为 pdf-layout-ocr-v2；已有中文 PDF 须重建成功后再验证连续词检索，无需重新上传原文件。

更新参数会生成新版本；新索引成功发布前，旧索引仍供检索。OCR 不可用、质量不足、超限或解析失败均不得替换旧索引。
「保留原文」仍可能执行本地 OCR，但不调用加工模型；OCR 不产生供应商 Token。索引嵌入费用由关键词/混合检索设置独立决定。
整个文件为空白时返回 ocr_required；混合文件中的空白页被跳过并计入摘要。

OCR 是提取结果，不等于原件逐字转录；置信度不是事实准确率。无边框/扫描复杂表格、图表含义仍须核对原页。
自动模式配置加工模型时，会加工含 OCR 或已识别表格的 PDF；纯文字 PDF 不自动调用加工模型。
""",
        encoding="utf-8",
    )
    print(root)


if __name__ == "__main__":
    main()
