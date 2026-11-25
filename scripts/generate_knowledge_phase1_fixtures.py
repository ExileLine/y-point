# -*- coding: utf-8 -*-
# @Time    : 2026-11-12 16:18:44
# @Author  : yangyuexiong
# @File    : generate_knowledge_phase1_fixtures.py

"""构造知识库第一阶段的确定性上传夹具，并用项目实际校验器核对预期。"""

import csv
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from app.core.enums import KnowledgeFailure
from app.services.artifact_store import ArtifactValidationError, validate_file
from app.services.knowledge_parser import KnowledgeProcessingError, chunk_knowledge, parse_knowledge

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "knowledge-phase1-manual"
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass(frozen=True)
class Formula:
    """显式声明公式及保存时的缓存，缺省缓存用于验证禁止擅自计算。"""

    expression: str
    cached: int | None = None


@dataclass
class Sheet:
    """描述测试工作表，直接构造协议文件以保持公式缓存与隐藏结构可控。"""

    name: str
    rows: list[list]
    header_rows: int = 1
    hidden: bool = False
    hidden_rows: set[int] = field(default_factory=set)
    hidden_columns: list[tuple[int, int]] = field(default_factory=list)
    merges: list[str] = field(default_factory=list)
    table_name: str | None = None


def column_name(number: int) -> str:
    """把正整数列号转为 Excel 字母，不依赖表格创作库。"""
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def write_bytes(relative: str, content: bytes) -> Path:
    """仅写入专用测试目录，保留其他项目文件与业务数据。"""
    target = OUTPUT / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def write_csv(relative: str, rows: list[list], *, delimiter: str = ",", bom: bool = False) -> Path:
    """用真实 CSV 引号与换行规则写出 UTF-8 数据。"""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\r\n")
    writer.writerows(rows)
    return write_bytes(relative, buffer.getvalue().encode("utf-8-sig" if bom else "utf-8"))


def cell_xml(reference: str, value: object, header: bool) -> str:
    """编码单元格，公式必须保留调用者指定缓存，不触发重算。"""
    style = 1 if header else 2 if isinstance(value, (int, float, Formula)) else 0
    prefix = f'<c r="{reference}" s="{style}"'
    if isinstance(value, Formula):
        cached = f"<v>{value.cached}</v>" if value.cached is not None else "<v/>"
        return f"{prefix}><f>{escape(value.expression)}</f>{cached}</c>"
    if isinstance(value, (int, float)):
        return f"{prefix}><v>{value}</v></c>"
    return f'{prefix} t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'


def worksheet_xml(sheet: Sheet) -> str:
    """写出含冻结表头、换行、合并及隐藏行列的标准工作表。"""
    width = max((len(row) for row in sheet.rows), default=1)
    last = f"{column_name(width)}{max(1, len(sheet.rows))}"
    columns = []
    for number in range(1, width + 1):
        if any(left <= number <= right for left, right in sheet.hidden_columns):
            continue
        size = 60 if number == width and width >= 4 else 25
        columns.append((number, f'<col min="{number}" max="{number}" width="{size}" customWidth="1"/>'))
    for left, right in sheet.hidden_columns:
        columns.append((left, f'<col min="{left}" max="{right}" width="25" hidden="1" outlineLevel="1" customWidth="1"/>'))
    column_xml = "".join(value for _, value in sorted(columns))
    rows = []
    for number, values in enumerate(sheet.rows, start=1):
        hidden = ' hidden="1"' if number in sheet.hidden_rows else ""
        cells = "".join(
            cell_xml(f"{column_name(column)}{number}", value, number <= sheet.header_rows) for column, value in enumerate(values, 1) if value is not None
        )
        rows.append(f'<row r="{number}" ht="38" customHeight="1"{hidden}>{cells}</row>')
    merges = ""
    if sheet.merges:
        merges = f'<mergeCells count="{len(sheet.merges)}">' + "".join(f"<mergeCell ref={quoteattr(area)}/>" for area in sheet.merges) + "</mergeCells>"
    table_parts = '<tableParts count="1"><tablePart r:id="rIdTable"/></tableParts>' if sheet.table_name else ""
    frozen = f'<pane ySplit="{sheet.header_rows}" topLeftCell="A{sheet.header_rows + 1}" activePane="bottomLeft" state="frozen"/>'
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{MAIN_NS}" xmlns:r="{REL_NS}">'
        f'<dimension ref="A1:{last}"/><sheetViews><sheetView showGridLines="0" workbookViewId="0">{frozen}</sheetView></sheetViews>'
        f'<sheetFormatPr defaultRowHeight="20"/><cols>{column_xml}</cols><sheetData>{"".join(rows)}</sheetData>{merges}{table_parts}</worksheet>'
    )


def write_workbook(relative: str, sheets: list[Sheet]) -> Path:
    """直接写最小完整 OOXML 测试夹具，公式缓存和错误样例均不经办公软件改写。"""
    buffer = io.BytesIO()
    sheet_entries, relationships, overrides = [], [], []
    styles = (
        f'<styleSheet xmlns="{MAIN_NS}"><numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts>'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/>'
        '<sz val="11"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF234D70"/>'
        '<bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1">'
        '<alignment vertical="center" wrapText="1"/></xf></cellXfs><cellStyles count="1">'
        '<cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
    )
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "_rels/.rels",
            f'<Relationships xmlns="{PACKAGE_REL_NS}"><Relationship Id="rIdWorkbook" Type="{REL_NS}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
        archive.writestr("xl/styles.xml", styles)
        for number, sheet in enumerate(sheets, 1):
            state = ' state="hidden"' if sheet.hidden else ""
            sheet_entries.append(f'<sheet name={quoteattr(sheet.name)} sheetId="{number}" r:id="rId{number}"{state}/>')
            relationships.append(f'<Relationship Id="rId{number}" Type="{REL_NS}/worksheet" Target="worksheets/sheet{number}.xml"/>')
            overrides.append(
                f'<Override PartName="/xl/worksheets/sheet{number}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            )
            archive.writestr(f"xl/worksheets/sheet{number}.xml", worksheet_xml(sheet))
            if sheet.table_name:
                last = f"{column_name(len(sheet.rows[0]))}{len(sheet.rows)}"
                headers = "".join(f'<tableColumn id="{column}" name={quoteattr(str(value))}/>' for column, value in enumerate(sheet.rows[0], 1))
                archive.writestr(
                    f"xl/tables/table{number}.xml",
                    f'<table xmlns="{MAIN_NS}" id="{number}" name="{sheet.table_name}" displayName="{sheet.table_name}" ref="A1:{last}" totalsRowShown="0">'
                    f'<autoFilter ref="A1:{last}"/><tableColumns count="{len(sheet.rows[0])}">{headers}</tableColumns>'
                    '<tableStyleInfo name="TableStyleMedium2" showFirstColumn="0" showLastColumn="0" showRowStripes="1" showColumnStripes="0"/></table>',
                )
                archive.writestr(
                    f"xl/worksheets/_rels/sheet{number}.xml.rels",
                    f'<Relationships xmlns="{PACKAGE_REL_NS}"><Relationship Id="rIdTable" '
                    f'Type="{REL_NS}/table" Target="../tables/table{number}.xml"/></Relationships>',
                )
                overrides.append(
                    f'<Override PartName="/xl/tables/table{number}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/>'
                )
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{MAIN_NS}" xmlns:r="{REL_NS}"><sheets>{"".join(sheet_entries)}</sheets>'
            '<calcPr calcMode="manual" fullCalcOnLoad="0" forceFullCalc="0"/></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<Relationships xmlns="{PACKAGE_REL_NS}">{"".join(relationships)}'
            f'<Relationship Id="rIdStyles" Type="{REL_NS}/styles" Target="styles.xml"/></Relationships>',
        )
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            f"{''.join(overrides)}</Types>",
        )
    return write_bytes(relative, buffer.getvalue())


def generate() -> None:
    """生成成功、特殊结构、更新及明确失败的测试文件，全部为虚构数据。"""
    write_bytes(
        "正常入库/01_报销规则.txt",
        "报销规则测试样本：出差结束后十五个工作日内提交报销申请，逾期需要负责人书面批准。\n唯一检索词：行舟报销基线。\n".encode(),
    )
    write_bytes(
        "正常入库/02_采购制度.md",
        "# 澄川采购制度（测试数据）\n\n## 询价要求\n采购钢材时至少保留两家供应商的报价原件，报价必须说明单价单位、供货周期和报价有效期。\n\n"
        "## 到货验收\n验收负责人核对型号、数量和质量等级，不得把试算金额当成最终合同价。\n\n## 标记\n唯一检索词：澄川采购基线。\n".encode(),
    )
    headers = ["供应商", "材料", "单价（元/吨）", "数量（吨）", "交货天数", "说明"]
    write_workbook(
        "正常入库/03_采购报价_单行表头.xlsx",
        [
            Sheet(
                "采购报价",
                [
                    headers,
                    ["青禾供应商", "热轧钢板", 3800, 50, 7, "适用于常规补货；唯一标记：青禾报价样本"],
                    ["远帆供应商", "热轧钢板", 3950, 30, 3, "提供加急配送；唯一标记：远帆报价样本"],
                    ["星桥供应商", "镀锌钢管", 4200, 20, 10, "型号必须与采购单一致；唯一标记：星桥报价样本"],
                ],
                table_name="ProcurementQuotes",
            ),
        ],
    )
    write_workbook(
        "表格专项/04_多级表头_隐藏内容_公式.xlsx",
        [
            Sheet(
                "报价",
                [
                    ["供应商", "报价", None, "质量", "内部备注"],
                    ["名称", "单价（元/吨）", "数量（吨）", "等级", "仅内部使用"],
                    ["青禾供应商", 3800, 50, "优等品", "HIDDEN_COLUMN_SENTINEL_8J2K"],
                    ["HIDDEN_ROW_SENTINEL_5P9X", 9999, 1, "隐藏测试行", "隐藏行备注"],
                    ["远帆供应商", Formula("B3*2", 7600), 20, "合格品", "内部条件"],
                    ["星桥供应商", Formula("B3*3"), 10, "合格品", "内部条件"],
                ],
                header_rows=2,
                hidden_rows={4},
                hidden_columns=[(5, 5)],
                merges=["B1:C1"],
            ),
            Sheet("内部底稿", [["字段", "值"], ["内部字段", "仅内部"], ["底稿", "HIDDEN_SHEET_SENTINEL_3Q7Z"]], hidden=True),
        ],
    )
    write_workbook(
        "表格专项/05_分组隐藏列.xlsx",
        [
            Sheet(
                "分组隐藏",
                [["公开名称", "内部编码", "内部备注", "公开分类"], ["明启材料", "GROUP_HIDDEN_B_7M4V", "GROUP_HIDDEN_C_6N8W", "办公耗材"]],
                hidden_columns=[(2, 3)],
            )
        ],
    )
    write_workbook(
        "表格专项/06_多工作表选择.xlsx",
        [
            Sheet("华东报价", [["供应商", "单价（元/吨）", "标记"], ["东岸材料", 4100, "华东专属样本"]]),
            Sheet("华南报价", [["供应商", "单价（元/吨）", "标记"], ["南湾材料", 4300, "华南专属样本"]]),
            Sheet("说明", [["项目", "内容"], ["测试说明", "说明专属样本；该工作表可用于验证选择范围"]]),
        ],
    )
    write_csv(
        "正常入库/07_中文_BOM_逗号与多行.csv",
        [
            ["记录编号", "客户", "金额（元）", "状态", "说明"],
            ["CSV001", "青禾商贸", "0", "待核对", '备注含逗号,并含双引号"原样保留"'],
            ["CSV002", "远帆商贸", "-120.50", "退款", "第一行：CSV多行样本\n第二行：必须属于同一条记录"],
            ["CSV003", "星桥商贸", "1250.75", "待确认", "保留小数与字段含义"],
        ],
        bom=True,
    )
    write_csv(
        "正常入库/08_分号分隔.csv",
        [["订单编号", "商品", "数量", "说明"], ["SEM001", "办公纸", "12", "分号分隔样本"], ["SEM002", "档案盒", "8", "字段内分号;必须保留"]],
        delimiter=";",
    )
    rows = [["记录编号", "业务对象", "数量", "说明"]]
    for number in range(1, 73):
        description = f"分页样本第{number:03d}条。" + "该记录用于验证分页读取、原始字段关联与单元格来源定位，翻页不得遗漏或重复记录。" * 6
        rows.append([f"PAGE{number:03d}", f"测试材料{number:03d}", number, description])
    write_csv("表格专项/09_分页预览_72条.csv", rows)
    for version, lead_time, marker in (("v1", 7, "版本一交货样本"), ("v2", 3, "版本二交货样本")):
        write_workbook(
            f"版本更新/{version}/交货规则.xlsx",
            [Sheet("交货规则", [["材料", "交货天数", "标记"], ["云杉测试材料", lead_time, marker]], table_name="DeliveryRules")],
        )
    write_bytes("预期失败/F01_字段数量不一致.csv", "名称,金额,说明\n甲,100,正常行\n乙,200\n".encode())
    write_csv("预期失败/F02_只有表头.csv", [["供应商", "单价", "数量"]])
    write_workbook("预期失败/F03_空工作表.xlsx", [Sheet("空表", [])])
    write_csv("预期失败/F04_单条记录超限.csv", [["名称", "说明"], ["超长样本", "超长完整记录" * 450]])
    write_bytes("预期失败/F05_损坏工作簿.xlsx", b"This is deliberately not an Office ZIP archive.")
    write_bytes("预期失败/F06_GB18030编码.csv", "名称,说明\n编码测试,该文件故意采用非UTF8编码\n".encode("gb18030"))


def verify() -> list[dict]:
    """用上传校验与实际解析、分块函数核对所有样例，严格区分上传和后台失败。"""
    results = []
    for path in sorted(OUTPUT.rglob("*")):
        if not path.is_file() or path.suffix not in {".txt", ".md", ".csv", ".xlsx"} or path.parent == OUTPUT:
            continue
        relative = path.relative_to(OUTPUT).as_posix()
        result = {
            "file": relative,
            "byte_size": path.stat().st_size,
        }
        try:
            media_type = validate_file(path, path.name, allow_csv=True)
        except ArtifactValidationError as error:
            assert path.name.startswith(("F05_", "F06_")), (relative, str(error))
            result.update(
                {
                    "expected": "upload_rejected",
                    "reason": str(error),
                },
            )
            results.append(result)
            continue
        options = {}
        if path.name.startswith("04_"):
            options = {
                "header_rows": 2,
                "sheet_names": ["报价"],
            }
        elif path.name.startswith("06_"):
            options = {
                "sheet_names": ["华东报价"],
            }
        try:
            parsed = parse_knowledge(path.read_bytes(), media_type, options)
            chunks = chunk_knowledge(parsed, 800, 100)
        except KnowledgeProcessingError as error:
            expected = KnowledgeFailure.SIZE_LIMIT if path.name.startswith("F04_") else KnowledgeFailure.INVALID_STRUCTURE
            assert path.name.startswith(("F01_", "F02_", "F03_", "F04_")) and error.code == expected, (relative, error)
            result.update(
                {
                    "expected": "job_failed",
                    "failure_code": error.code,
                },
            )
            results.append(result)
            continue
        assert not path.name.startswith("F"), relative
        assert chunks and all(chunk["content"] == parsed.text[chunk["start_offset"] : chunk["end_offset"]] for chunk in chunks)
        if path.name.startswith("04_"):
            assert "HIDDEN_" not in parsed.text and "报价 / 单价（元/吨）[B]" in parsed.text
            assert "公式：=B3*2；缓存结果：7600" in parsed.text and "公式：=B3*3；缓存结果：未提供，未计算" in parsed.text
        elif path.name.startswith("05_"):
            assert "GROUP_HIDDEN" not in parsed.text and chunks[0]["source_locator"]["columns"] == ["A", "D"]
        elif path.name.startswith("06_"):
            assert "华东专属样本" in parsed.text and "华南专属样本" not in parsed.text and "说明专属样本" not in parsed.text
        elif path.name.startswith("07_"):
            assert len(parsed.blocks) == 3 and parsed.blocks[1]["source_locator"]["range"] == "A3:E4"
            assert '金额（元）[C]="0"' in parsed.text and "-120.50" in parsed.text
        elif path.name.startswith("09_"):
            assert len(parsed.blocks) == 72 and len(chunks) > 20
        result.update(
            {
                "expected": "ready",
                "parser_options": options,
                "records_or_blocks": len(parsed.blocks),
                "chunks_at_800": len(chunks),
            },
        )
        results.append(result)
    assert len(results) == 17, len(results)
    write_bytes("校验结果.json", (json.dumps(results, ensure_ascii=False, indent=2) + "\n").encode())
    return results


def write_package(results: list[dict]) -> Path:
    """打包已校验样例和操作说明，不包含目录中额外保存的人工截图或日志。"""
    target = OUTPUT.with_suffix(".zip")
    relatives = [row["file"] for row in results] + ["README.md", "验收记录.md", "校验结果.json"]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in relatives:
            archive.write(OUTPUT / relative, f"{OUTPUT.name}/{relative}")
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None and len(archive.infolist()) == len(relatives)
    return target


def main() -> None:
    """生成夹具并打印精简校验汇总，不连接业务库或调用外部模型。"""
    generate()
    results = verify()
    print(f"已生成并校验 {len(results)} 个上传样例：{OUTPUT}")
    for row in results:
        print(f"{row['expected']:16} {row['byte_size']:7} B  {row['file']}")
    print(f"完整测试包：{write_package(results)}")


if __name__ == "__main__":
    main()
