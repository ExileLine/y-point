# -*- coding: utf-8 -*-
# @Time    : 2026-11-05 09:57:52
# @Author  : yangyuexiong
# @File    : skill_reports.py

"""固定 Skill 模板的校验、渲染和来源核对；不执行用户模板代码。"""

import asyncio
import hashlib
import io
import json
import re
import zipfile
import unicodedata
from copy import deepcopy
from itertools import islice
from xml.etree import ElementTree

from docx import Document
from docxtpl import DocxTemplate
from jinja2 import StrictUndefined, nodes
from jinja2.sandbox import SandboxedEnvironment
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from langchain_core.tools import ToolException, tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ArtifactOrigin, ArtifactState, ToolFeedbackFailure
from app.models.artifact import Artifact
from app.models.skill import RunSkill
from app.models.skill_template import SkillTemplate
from app.services.artifact_store import artifact_root, stage_generated_artifact
from app.services.private_blob_store import read_blob
from app.services.tool_registry import ToolContext
from app.services.tool_arguments import ToolArgumentsError

MAX_TEMPLATE_BYTES = 2 * 1024 * 1024


class ReportEnvironment(SandboxedEnvironment):
    """让 JSON 字段优先于同名 Python 方法，并保留沙箱属性检查。"""

    def getattr(self, obj, attribute):
        """模板的点访问优先读取 JSON 对象字段。"""
        if isinstance(obj, dict) and attribute in obj:
            return obj[attribute]
        return super().getattr(obj, attribute)


MAX_SKILL_TEMPLATES = 10


def template_key(filename: str) -> str:
    """规范化资源文件名，禁止目录路径并按不区分大小写的名称定位。"""
    key = unicodedata.normalize("NFC", filename).casefold()
    if not key.endswith(".docx") or len(key) > 255 or any(char in filename for char in ("/", "\\", "\x00")):
        raise ValueError("模板必须使用不含目录的 DOCX 文件名，最多 255 字符")
    return key


def report_package_hash(files: dict, templates: dict | list[dict]) -> str:
    """按文本与有序模板资源集合计算稳定指纹，兼容旧单模板调用。"""
    entries = [templates] if isinstance(templates, dict) else templates
    resources = [
        {
            "template_key": entry.get("template_key") or template_key(entry["filename"]),
            "sha256": entry["sha256"],
            "fields_schema": entry["fields_schema"],
        }
        for entry in entries
    ]
    return hashlib.sha256(
        json.dumps(
            {
                "files": files,
                "templates": sorted(resources, key=lambda entry: entry["template_key"]),
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode(),
    ).hexdigest()


def report_environment() -> SandboxedEnvironment:
    """只开放字段、条件和有界循环，不开放调用、导入及全局对象。"""
    environment = ReportEnvironment(undefined=StrictUndefined, autoescape=True)
    environment.globals.clear()
    environment.filters.clear()
    return environment


def validate_fields_schema(schema: dict) -> dict:
    """限制结构深度、字段数、数组和文本上限，禁止引用和复杂匹配。"""
    result = deepcopy(schema)
    count = 0

    def inspect(node: dict, depth: int) -> None:
        """递归验证模板字段契约并补齐有限的输出预算。"""
        nonlocal count
        count += 1
        if not isinstance(node, dict) or depth > 6 or count > 128:
            raise ValueError("字段结构过深或字段过多")
        allowed = {"type", "properties", "required", "additionalProperties", "items", "maxItems", "maxLength", "description", "enum"}
        if set(node) - allowed:
            raise ValueError("字段契约仅支持基础类型、对象、数组、必填项和长度约束")
        kind = node.get("type")
        if kind == "object":
            properties = node.get("properties", {})
            if not isinstance(properties, dict) or len(properties) > 64:
                raise ValueError("对象字段过多")
            node["additionalProperties"] = False
            for key, child in properties.items():
                if not isinstance(key, str) or not key.isidentifier() or key.startswith("_"):
                    raise ValueError("字段名称必须是非下划线开头的标识符")
                inspect(child, depth + 1)
        elif kind == "array":
            node.setdefault("maxItems", 100)
            if not isinstance(node["maxItems"], int) or not 0 <= node["maxItems"] <= 100:
                raise ValueError("数组最多 100 项")
            inspect(node.get("items"), depth + 1)
        elif kind == "string":
            node.setdefault("maxLength", 8000)
            if not isinstance(node["maxLength"], int) or not 1 <= node["maxLength"] <= 8000:
                raise ValueError("单个文本字段最多 8000 字符")
        elif kind not in {"integer", "number", "boolean", "null"}:
            raise ValueError("字段类型不支持")

    inspect(result, 0)
    if result.get("type") != "object" or not result.get("properties"):
        raise ValueError("报告字段必须是包含属性的对象")
    try:
        Draft202012Validator.check_schema(result)
    except SchemaError as exc:
        raise ValueError("字段契约不是有效的 JSON Schema") from exc
    return result


def validate_docx_template(data: bytes, schema: dict) -> None:
    """拒绝压缩炸弹、活动内容及可执行模板语法，核对模板变量契约。"""
    if not data or len(data) > MAX_TEMPLATE_BYTES:
        raise ValueError("DOCX 模板最多 2 MiB")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > 512 or sum(item.file_size for item in infos) > 10 * 1024 * 1024:
            raise ValueError("模板解压内容超过限制")
        if len({item.filename for item in infos}) != len(infos):
            raise ValueError("模板包含重复文件")
        for item in infos:
            name = item.filename.lower()
            if any(part == ".." for part in name.split("/")) or name.startswith("/") or "\\" in name or item.flag_bits & 1:
                raise ValueError("模板包含非法文件路径")
            if any(part in name for part in ("vbaproject", "activex", "embeddings/", "altchunk")):
                raise ValueError("模板不能包含宏、活动控件或嵌入对象")
            if name.endswith((".xml", ".rels")):
                xml = archive.read(item)
                if any(token in xml.lower() for token in (b"macroenabled", b":altchunk", b"oleobject", b"activex")):
                    raise ValueError("模板包含活动文档内容")
            if name.endswith((".xml", ".rels")) and b"<!" in archive.read(item):
                raise ValueError("模板不允许 XML 声明实体或 DTD")
            supported = name == "word/document.xml" or re.fullmatch(r"word/(?:header|footer)\d+\.xml", name) is not None
            if name.endswith(".xml") and not supported:
                xml = archive.read(item).decode("utf-8")
                text = "".join(ElementTree.fromstring(xml).itertext())
                if any(marker in xml or marker in text for marker in ("{{", "{%", "{#")):
                    raise ValueError("文档属性、脚注及其他部件不支持模板语法，请仅在正文、页眉或页脚设置字段")
            if name.endswith(".rels"):
                relations = ElementTree.fromstring(archive.read(item))
                if any(relation.attrib.get("TargetMode", "").lower() == "external" for relation in relations):
                    raise ValueError("模板不能引用外部资源")
        if "word/document.xml" not in archive.namelist():
            raise ValueError("文件不是 DOCX 模板")
    document = DocxTemplate(io.BytesIO(data))
    document.init_docx()
    environment = report_environment()
    # 与渲染器使用同一套预处理，兼容 docxtpl 的段落和表格行标记。
    parts = [document.get_xml()]
    parts.extend(part.blob.decode("utf-8") for part in document.docx.part.package.parts if "/header" in part.partname or "/footer" in part.partname)
    variables = set()
    from jinja2 import meta

    rendered_budget = 0
    for xml in parts:
        syntax = environment.parse(document.patch_xml(xml))
        allowed_nodes = (
            nodes.Template,
            nodes.Output,
            nodes.TemplateData,
            nodes.Name,
            nodes.Const,
            nodes.Getattr,
            nodes.Getitem,
            nodes.For,
            nodes.If,
            nodes.Compare,
            nodes.Operand,
            nodes.And,
            nodes.Or,
            nodes.Not,
            nodes.List,
            nodes.Tuple,
        )
        for node in syntax.find_all(nodes.Node):
            if not isinstance(node, allowed_nodes):
                raise ValueError("模板仅支持字段替换、条件和循环，不支持调用、过滤器或导入")
        loops = list(syntax.find_all(nodes.For))
        if len(loops) > 16 or any(
            list(loop.iter.find_all(nodes.Node)) and not isinstance(loop.iter, (nodes.Name, nodes.Getattr, nodes.Getitem)) for loop in loops
        ):
            raise ValueError("模板循环必须直接读取报告数组")
        if any(any(isinstance(child, nodes.For) or list(child.find_all(nodes.For)) for child in loop.body) for loop in loops):
            raise ValueError("模板不允许嵌套循环")

        def field_schema(expression, scope: dict) -> dict:
            """解析静态字段路径，禁止循环任意对象或字符串。"""
            if isinstance(expression, nodes.Name):
                return scope.get(expression.name, {})
            if isinstance(expression, nodes.Getattr):
                return field_schema(expression.node, scope).get("properties", {}).get(expression.attr, {})
            if isinstance(expression, nodes.Getitem) and isinstance(expression.arg, nodes.Const):
                return field_schema(expression.node, scope).get("properties", {}).get(expression.arg.value, {})
            return {}

        def estimate(statements, scope: dict) -> int:
            """按最坏字段转义和循环次数计算有界 XML 输出预算。"""
            size = 0
            for statement in statements:
                if isinstance(statement, nodes.TemplateData):
                    size += len(statement.data.encode())
                elif isinstance(statement, nodes.Output):
                    size += estimate(statement.nodes, scope)
                elif isinstance(statement, nodes.For):
                    array = field_schema(statement.iter, scope)
                    if array.get("type") != "array" or not isinstance(statement.target, nodes.Name):
                        raise ValueError("循环只能遍历契约中声明的数组并使用单个循环变量")
                    nested = dict(scope)
                    nested[statement.target.name] = array["items"]
                    size += array["maxItems"] * estimate(statement.body, nested) + estimate(statement.else_, scope)
                elif isinstance(statement, nodes.If):
                    size += estimate(statement.body + statement.else_ + statement.elif_, scope)
                else:
                    field = field_schema(statement, scope)
                    size += 6 * field.get("maxLength", 64 * 1024)
            return size

        rendered_budget += estimate(syntax.body, schema["properties"])
        if rendered_budget > 2 * 1024 * 1024:
            raise ValueError("模板输出预算超过 2 MiB，请缩小字段长度、列表长度或模板正文")
        variables.update(meta.find_undeclared_variables(syntax))
    if not variables or not variables <= set(schema["properties"]):
        raise ValueError("模板存在未声明字段或没有占位字段")
    Document(io.BytesIO(data))


async def templates_for_version(db: AsyncSession, skill_id: int, version: int) -> list[SkillTemplate]:
    """读取精确版本的全部模板，不自动跟随最新版本。"""
    return list(
        await db.scalars(
            select(SkillTemplate).where(SkillTemplate.skill_id == skill_id, SkillTemplate.skill_version == version).order_by(SkillTemplate.template_key),
        ),
    )


async def template_for_version(db: AsyncSession, skill_id: int, version: int, template_name: str | None = None) -> SkillTemplate | None:
    """按文件名选择固定模板；旧单模板可省略名称，多模板禁止猜测。"""
    templates = await templates_for_version(db, skill_id, version)
    if template_name is not None and not template_name.strip():
        template_name = None
    if template_name is not None:
        from app.services.skill_packages import package_path

        key = package_path(template_name).casefold()
        exact = next((item for item in templates if item.template_key == key), None)
        if exact is not None:
            return exact
        matches = [item for item in templates if item.template_key.rsplit("/", 1)[-1] == key]
        if len(matches) > 1:
            raise ValueError("模板文件名重复，请指定完整的 assets/templates/ 路径")
        return matches[0] if matches else None
    if len(templates) > 1:
        raise ValueError("此 Skill 有多个模板，请按 SKILL.md 指定 template_name")
    return templates[0] if templates else None


async def copy_template(db: AsyncSession, skill_id: int, previous_version: int, next_version: int) -> None:
    """正文更新时沿用全部不可变模板引用，不复制二进制正文。"""
    previous = await templates_for_version(db, skill_id, previous_version)
    db.add_all(
        [
            SkillTemplate(
                skill_id=skill_id,
                skill_version=next_version,
                template_key=item.template_key,
                artifact_id=item.artifact_id,
                fields_schema=item.fields_schema,
            )
            for item in previous
        ],
    )


async def activate_skill_report(db: AsyncSession, run_id: str, skill_id: int) -> None:
    """模型成功读取模板 Skill 正文后，在同一租约事务中登记本轮交付要求。"""
    fixed = await db.scalar(
        select(RunSkill)
        .join(SkillTemplate, (RunSkill.skill_id == SkillTemplate.skill_id) & (RunSkill.skill_version == SkillTemplate.skill_version))
        .where(RunSkill.run_id == run_id, RunSkill.skill_id == skill_id),
    )
    if fixed is not None:
        fixed.required_report = True


async def required_report_skill_ids(db: AsyncSession, run_id: str) -> list[int]:
    """读取本轮实际执行且要求模板交付的 Skill，未使用的绑定不会触发生成。"""
    return list(await db.scalars(select(RunSkill.skill_id).where(RunSkill.run_id == run_id, RunSkill.required_report.is_(True)).order_by(RunSkill.id)))


async def required_report_delivered(db: AsyncSession, run_id: str) -> bool:
    """核对所有已执行模板 Skill 的固定版本产物，通用 DOCX 不能冒充。"""
    required = (
        await db.execute(
            select(RunSkill, SkillTemplate.artifact_id)
            .outerjoin(SkillTemplate, (RunSkill.skill_id == SkillTemplate.skill_id) & (RunSkill.skill_version == SkillTemplate.skill_version))
            .where(RunSkill.run_id == run_id, RunSkill.required_report.is_(True)),
        )
    ).all()
    if not required:
        return True
    artifacts = (
        await db.scalars(
            select(Artifact).where(
                Artifact.run_id == run_id,
                Artifact.source == ArtifactOrigin.GENERATED,
                Artifact.state == ArtifactState.PENDING,
                Artifact.is_deleted == 0,
            ),
        )
    ).all()
    delivered = {
        (item.template_source.get("skill_id"), item.template_source.get("skill_version"), item.template_source.get("template_artifact_id"))
        for item in artifacts
        if item.template_source is not None
    }
    expected = {(fixed.skill_id, fixed.skill_version) for fixed, _ in required}
    satisfied = {
        (fixed.skill_id, fixed.skill_version)
        for fixed, template_id in required
        if template_id is not None and (fixed.skill_id, fixed.skill_version, template_id) in delivered
    }
    return expected <= satisfied


async def _report_template_for_version(db: AsyncSession, skill_id: int, version: int, template_name: str | None) -> SkillTemplate:
    """只将固定版本模板的名称选择错误反馈给模型，缺失资源仍作为真实故障处理。"""
    try:
        selected = await template_for_version(db, skill_id, version, template_name)
    except ValueError:
        selected = None
    if selected is not None:
        return selected
    templates = await templates_for_version(db, skill_id, version)
    if not templates:
        raise ValueError("此 Skill 固定版本中没有报告模板")
    raise ToolArgumentsError(
        json.dumps(
            {
                "error": ToolFeedbackFailure.INVALID_ARGUMENTS,
                "message": "报告尚未生成。单模板可省略 template_name 或传空值；多模板必须从当前固定版本的模板列表中指定完整名称。",
                "retryable": True,
                "allowed_templates": [item.template_key for item in templates],
                "issues": [
                    {
                        "path": "/template_name",
                        "rule": "template_selection",
                    },
                ],
            },
            ensure_ascii=False,
        ),
    )


def skill_document_tool(context: ToolContext):
    """创建只能使用本轮已授权 Skill 模板的幂等暂存工具。"""

    @tool
    async def render_skill_document(skill_id: int, report_data: dict, filename: str, template_name: str | None = None) -> str:
        """按本轮固定模板生成 DOCX；单模板省略 template_name 或传空值，多模板指定 SKILL.md 中的完整模板文件名。"""
        from app.services.skills import selected_skills

        canonical = json.dumps(report_data, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        if not filename.lower().endswith(".docx") or len(canonical) > 64 * 1024:
            raise ValueError("报告必须是 DOCX，结构化数据最多 64 KiB")
        async with context.sessions() as db:
            await context.require_active(db, enabled=lambda run: run.session_id is not None)
            available = await selected_skills(db, [skill_id], context.owner_id)
            fixed = await db.scalar(select(RunSkill).where(RunSkill.run_id == context.run_id, RunSkill.skill_id == skill_id))
            if fixed is None:
                raise ValueError("Skill 未绑定本轮运行")
            template = await _report_template_for_version(db, skill_id, fixed.skill_version, template_name)
            source = await db.get(Artifact, template.artifact_id)
            if source is None or source.source != ArtifactOrigin.SKILL_RESOURCE or source.state != ArtifactState.READY or source.is_deleted:
                raise ValueError("模板文件不可用")
            if source.owner_id != available[0].owner_id or source.status != 1:
                raise ValueError("模板文件归属无效")
            raw = await read_blob(artifact_root(), source.storage_key, max_bytes=MAX_TEMPLATE_BYTES)
            if hashlib.sha256(raw).hexdigest() != source.sha256:
                raise ValueError("模板完整性校验失败")
            schema = template.fields_schema
            provenance = {
                "skill_id": skill_id,
                "skill_name": available[0].name,
                "skill_version": fixed.skill_version,
                "template_artifact_id": source.id,
                "template_key": template.template_key,
                "template_name": source.filename,
                "template_sha256": source.sha256,
                "report_data_sha256": hashlib.sha256(canonical).hexdigest(),
            }
        issues = [
            {
                "path": "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in error.absolute_path),
                "rule": error.validator,
            }
            for error in islice(Draft202012Validator(schema).iter_errors(report_data), 20)
        ]
        if issues:
            raise ToolException(
                json.dumps(
                    {
                        "error": ToolFeedbackFailure.INVALID_ARGUMENTS,
                        "message": "报告尚未生成。请按模板字段契约修正；业务校验工具若返回 report_data，应沿用该对象，不要添加额外字段。",
                        "retryable": True,
                        "allowed_fields": list(schema.get("properties", {})),
                        "required_fields": schema.get("required", []),
                        "issues": issues,
                    },
                    ensure_ascii=False,
                ),
            )

        def render() -> bytes:
            """使用无全局对象的模板环境渲染并重开校验。"""
            document = DocxTemplate(io.BytesIO(raw))
            document.render(report_data, jinja_env=report_environment(), autoescape=True)
            output = io.BytesIO()
            document.save(output)
            if output.tell() > 10 * 1024 * 1024:
                raise ValueError("生成报告超过 10 MiB")
            # 固定 ZIP 元数据，保证重试使用相同文件名与数据时字节一致。
            stable = io.BytesIO()
            with (
                zipfile.ZipFile(io.BytesIO(output.getvalue())) as source_zip,
                zipfile.ZipFile(stable, "w", zipfile.ZIP_DEFLATED) as target_zip,
            ):
                for name in sorted(source_zip.namelist()):
                    info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    target_zip.writestr(info, source_zip.read(name))
            result = stable.getvalue()
            Document(io.BytesIO(result))
            # StrictUndefined 核对模板字段；报告数据中的花括号按普通文字保留。
            return result

        data = await asyncio.to_thread(render)
        # 渲染后再次验证当前授权，避免长任务期间撤权。
        async with context.sessions() as db:
            await context.require_active(db, enabled=lambda run: run.session_id is not None)
            await selected_skills(db, [skill_id], context.owner_id)
        artifact = await stage_generated_artifact(
            context.sessions,
            context.run_id,
            context.owner_id,
            context.lease_token,
            filename,
            data,
            artifact_root(),
            template_source=provenance,
        )
        return json.dumps(
            {
                "artifact_id": artifact.id,
                "filename": artifact.filename,
                "template_source": provenance,
            },
            ensure_ascii=False,
        )

    render_skill_document.handle_tool_error = True
    return render_skill_document
