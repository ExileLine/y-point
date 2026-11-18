# -*- coding: utf-8 -*-
# @Time    : 2026-11-18 15:27:34
# @Author  : yangyuexiong
# @File    : presentation_artifacts.py
"""把结构化幻灯片生成真实、可编辑的 PowerPoint 文件。"""

import io
import math
import unicodedata
from typing import Annotated
from zipfile import ZipFile

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR
from pptx.util import Inches, Pt
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_PRESENTATION_SLIDES = 50
MAX_PRESENTATION_CHARS = 100_000


class ArtifactSlideTable(BaseModel):
    """一页中的简单表格，列名及每行单元格数量必须一致。"""

    model_config = ConfigDict(extra="forbid")
    headers: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        min_length=1,
        max_length=6,
        description="表头，最多六列",
        examples=[["项目", "金额"]],
    )
    rows: list[list[Annotated[str, Field(max_length=160)]]] = Field(
        min_length=1,
        max_length=10,
        description="数据行，最多十行，每行列数与表头一致",
        examples=[[["营收", "120万元"]]],
    )

    @model_validator(mode="after")
    def validate_columns(self):
        """拒绝缺列或多列，避免静默丢弃模型提供的单元格。"""
        if any(len(row) != len(self.headers) for row in self.rows):
            raise ValueError("幻灯片表格每行列数必须与表头一致")
        return self


class ArtifactSlide(BaseModel):
    """一页演示文稿的标题、正文、要点、可选表格和演讲者备注。"""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=100, description="幻灯片标题，建议不超过三十个汉字", examples=["季度经营汇报"])
    content: str = Field(default="", max_length=2000, description="可选正文；纯文本，不解析 Markdown 或 HTML", examples=["本季度按计划完成交付。"])
    bullets: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(
        default_factory=list,
        max_length=8,
        description="要点列表，最多八项",
        examples=[["营收增长", "交付及时"]],
    )
    table: ArtifactSlideTable | None = Field(default=None, description="可选简单表格，过长内容应拆分为多页", examples=[None])
    notes: str = Field(default="", max_length=4000, description="演讲者备注，保存在备注区，不混入幻灯片正文", examples=["补充说明本季度数据口径。"])


def _line_count(text: str, width: int) -> int:
    """按中西文显示宽度保守估算换行，拒绝明显超出单页的内容。"""
    return sum(max(1, math.ceil(sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in line) / width)) for line in text.split("\n"))


def _format_paragraph(paragraph, text: str, size: int, *, bold: bool = False, color: str = "243247") -> None:
    """统一设置可编辑文字，指定中文字体并保留原始内容。"""
    paragraph.text = text
    paragraph.font.name = "Microsoft YaHei"
    paragraph.font.size = Pt(size)
    paragraph.font.bold = bold
    paragraph.font.color.rgb = RGBColor.from_string(color)
    paragraph.space_after = Pt(10)
    paragraph.line_spacing = Pt(size * 1.2)


def render_presentation_artifact(slides: list[ArtifactSlide]) -> bytes:
    """生成十六比九演示文稿；不下载资源、不执行脚本、不静默裁掉正文。"""
    if not 1 <= len(slides) <= MAX_PRESENTATION_SLIDES:
        raise ValueError(f"演示文稿需要 1 至 {MAX_PRESENTATION_SLIDES} 页")
    if sum(len(slide.model_dump_json()) for slide in slides) > MAX_PRESENTATION_CHARS:
        raise ValueError("演示文稿内容超过长度限制")
    presentation = Presentation()
    presentation.slide_width = Inches(13.333)
    presentation.slide_height = Inches(7.5)
    presentation.core_properties.title = slides[0].title
    presentation.core_properties.author = "Y-Point"
    for index, item in enumerate(slides, 1):
        if not item.title.strip() or any(not value.strip() for value in item.bullets):
            raise ValueError("幻灯片标题和要点不能为空白")
        if _line_count(item.title, 56) > 2:
            raise ValueError(f"第 {index} 页标题过长，请精简标题")
        paragraphs = ([item.content] if item.content else []) + ["• " + value for value in item.bullets]
        text_lines = sum(_line_count(value, 90) for value in paragraphs)
        text_height = (text_lines * 0.31 + len(paragraphs) * 0.14) if paragraphs else 0
        table_heights = []
        if item.table:
            table_heights = [
                max(_line_count(value, max(12, 96 // len(item.table.headers))) for value in row) * 0.25 + 0.16 for row in [item.table.headers, *item.table.rows]
            ]
        if text_height + sum(table_heights) + (0.2 if paragraphs and item.table else 0) > 5.4:
            raise ValueError(f"第 {index} 页内容过多，请拆分为多页或把说明放入 notes")
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = RGBColor.from_string("F6F8FC")
        title_shape = slide.shapes.title
        title_shape.left, title_shape.top = Inches(0.7), Inches(0.4)
        title_shape.width, title_shape.height = Inches(11.9), Inches(1.1)
        title = title_shape.text_frame
        title.word_wrap = True
        title.margin_left = title.margin_right = title.margin_top = title.margin_bottom = 0
        _format_paragraph(title.paragraphs[0], item.title, 30, bold=True, color="143E70")
        top = 1.55
        if paragraphs:
            frame = slide.shapes.add_textbox(Inches(0.7), Inches(top), Inches(11.9), Inches(text_height + 0.1)).text_frame
            frame.word_wrap = True
            frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
            for number, value in enumerate(paragraphs):
                _format_paragraph(frame.paragraphs[0] if number == 0 else frame.add_paragraph(), value, 18)
            top += text_height + 0.2
        if item.table:
            table = slide.shapes.add_table(
                len(item.table.rows) + 1,
                len(item.table.headers),
                Inches(0.7),
                Inches(top),
                Inches(11.9),
                Inches(sum(table_heights)),
            ).table
            table.first_row = True
            for row_index, values in enumerate([item.table.headers, *item.table.rows]):
                table.rows[row_index].height = Inches(table_heights[row_index])
                for column, value in enumerate(values):
                    cell = table.cell(row_index, column)
                    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                    cell.margin_left = cell.margin_right = Inches(0.08)
                    cell.margin_top = cell.margin_bottom = Inches(0.04)
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = RGBColor.from_string("143E70" if row_index == 0 else "FFFFFF" if row_index % 2 else "E8EEF6")
                    cell.text_frame.word_wrap = True
                    _format_paragraph(cell.text_frame.paragraphs[0], value, 14, bold=row_index == 0, color="FFFFFF" if row_index == 0 else "243247")
                    cell.text_frame.paragraphs[0].space_after = 0
        footer = slide.shapes.add_textbox(Inches(11.8), Inches(7.05), Inches(0.8), Inches(0.25)).text_frame
        _format_paragraph(footer.paragraphs[0], f"{index} / {len(slides)}", 10, color="66758A")
        if item.notes:
            slide.notes_slide.notes_text_frame.text = item.notes
    output = io.BytesIO()
    presentation.save(output)
    # 固定包内时间戳，使同一输入的工具重试得到相同字节及暂存摘要。
    canonical = io.BytesIO()
    with ZipFile(output) as source, ZipFile(canonical, "w") as target:
        for entry in source.infolist():
            entry.date_time = (1980, 1, 1, 0, 0, 0)
            target.writestr(entry, source.read(entry.filename))
    return canonical.getvalue()
