"""明确文件交付请求的判定边界。"""

import pytest

from app.services.artifact_intent import requested_artifact_suffixes


@pytest.mark.parametrize(("text", "expected"), [
    ("帮我总结这个文档然后生成一个.txt文档", frozenset({".txt"})),
    ("请生成一份 Word 文档和 PDF 文件", frozenset({".docx", ".pdf"})),
    ("将结果导出为 Excel 表格", frozenset({".xlsx"})),
    ("根据材料生成一个可下载的报告", frozenset()),
    ("Create a report from the attached notes", frozenset()),
    ("请总结这个 PDF 文件", None),
    ("读取刚才生成的文件，再告诉我标题", None),
    ("根据刚才生成的文件，再生成一份 PDF", frozenset({".pdf"})),
    ("请告诉我如何生成一个 docx 文件", None),
    ("不要生成 txt 文件，只回答问题", None),
])
def test_requested_artifact_suffixes(text: str, expected: frozenset[str] | None) -> None:
    assert requested_artifact_suffixes(text) == expected
