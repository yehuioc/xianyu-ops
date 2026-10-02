"""Product-scoped reply defaults and paid service intake for the owned console.

The local database is authoritative after setup; reapplying preserves edited replies.
No customer message or marketplace write is performed by this module.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from .commerce import read_catalog, source_file
from .store import Store, now, product_key
from .paths import ITEM_PROFILES


INSTALLER = str(ITEM_PROFILES.get("installer") or "")
RESUME = str(ITEM_PROFILES.get("resume") or "")
ORANGEBOOKS = tuple(str(item) for item in ITEM_PROFILES.get("orangebooks", []))
RESUME_SERVICES = tuple(str(item) for item in ITEM_PROFILES.get("resume_services", []))
SERVICE_SPECS = ("人工安装调通", "安装+3个插件", "插件适配定制")
USAGE = {
    "career-kit": "电脑解压后用浏览器打开 HTML，可编辑后打印为 PDF；Markdown 模板可用文本编辑器填写。没有人工代写、代投或录用承诺。",
    "internship-kit": "电脑解压后用浏览器打开 HTML，按真实经历填写日报、周报与报告，按学校要求调整。示范内容为虚构，不提供代写或盖章。",
    "report-slides": "PPTX 用支持该格式的演示软件打开并编辑，先把示例数据换成自己的数据；HTML 提纲用浏览器打开。不含定制排版。",
    "kids-printables": "PDF 可查看和按 A4 打印，HTML 可在电脑浏览器编辑；儿童活动需成人陪同。不含实物或教材扫描件。",
    "focus-quiz": "解压后用电脑或手机浏览器打开 HTML，问卷离线运行，结果可打印。这是自我反思与娱乐工具，不是心理诊断。手机需能把本地 HTML 交给浏览器打开。",
    "couple-cards": "解压后用浏览器打开 HTML，可随机抽卡、跳过与打印。双方自愿参加，不想回答可直接跳过。手机需支持在浏览器打开本地 HTML。",
    "research-library": "在装有 Python 的电脑上按包内说明运行编译器，把自己的 CSV 资料整理成可搜索 HTML 和 Markdown 卡片。不附论文、书籍全文或模型服务。",
    "content-workflow": "解压后按说明阅读 Markdown 工作流与模板，将自己的材料填入需求、证据和审校表。需要 AI 协助时自备工具，不包含自动发布或模型额度。",
}
INTAKE = {
    "excel-cleaning": "请在当前会话提供：脱敏样表、要保留的字段、用于匹配的编号、期望结果示例和截止时间。基础范围为两张表、总计不超过 5000 行、一个匹配键；复杂公式、宏和恢复数据需要另行核对。",
    "website-service": "请在当前会话提供：网页用途、参考风格、三个内容区的文案、你有权使用的图片及期望时间；如需发布，再说明目标平台。基础范围为单页展示与手机适配，交付源码和部署说明；域名、服务器另计，不含支付和会员系统。",
    "automation-setup": "请在当前会话提供：电脑系统及版本、工具名称、要完成的一个实际任务、当前进度和脱敏报错。基础范围为一台电脑、一个工具或一个本地流程。账号登录、API Key 和验证码由你自己填写，请勿发给卖家。",
}
AFTER_SALE = "请在当前订单会话提供订单号、所选版本、文件名和错误提示，卖家核对后处理。不要重复购买或发送密码、密钥、验证码。这是自动答复，不表示已核实、补发或批准退款。"
DIGITAL_SHIPPING = "付款后，后台核实订单与规格再在当前闲鱼会话发送对应下载信息，无需邮寄。夸克交付需能使用夸克下载；请先确认文件格式适用。若已付款未收到，请发送“未收到”和订单号。"
SERVICE_SHIPPING = "这是按需求制作的服务。付款后自动发送需求清单，收到资料后由卖家确认范围、工期与交付；完成前不会把样例当成你的成品或自动确认发货。请先沟通再拍。"
INSTALLER_INTAKE = "请在当前会话提供 Windows 版本、Node.js 情况、目标效果、当前报错文字（先去掉密钥和个人信息）。插件服务请补充插件名称与来源链接。卖家核对所购规格、可做范围和处理时间后再开展；API 费用自理，密码、API Key、验证码请自行填写。"
RESUME_INTAKE = "请在当前会话提供一页脱敏简历或一个项目材料、你实际参与的工作、已有成果证据；有目标岗位时可附真实 JD。发送前删除姓名、电话、邮箱、微信和证件信息。先按所购档位确认材料与范围，再由卖家诊断和整理，不编造经历、不保证面试或录用。"


def _profiles(store: Store, account: str, only_item: str | None = None) -> list[dict]:
    profiles = []
    for spec in read_catalog()["products"]:
        publication = store.get("publication", product_key(account, spec["slug"]), {})
        if publication.get("state") != "published":
            continue
        item_id = str(publication.get("item_id") or "")
        if only_item and item_id != only_item:
            continue
        product = store.get("product", product_key(account, item_id), {})
        if publication.get("account") != account or product.get("account") != account:
            raise ValueError("发布记录的账号与商品不一致")
        if spec["sale_type"] not in {"digital", "service"} or spec.get("limitations"):
            raise ValueError("已发布商品缺少可执行的交付方式")
        listing = json.loads(source_file(spec["slug"], "listing", "listing.json").read_text(encoding="utf-8"))
        about = listing["description"].split("\n\n电子资料")[0].split("\n\n先在平台")[0]
        intake = listing.get("support_intake") or INTAKE.get(spec["slug"])
        usage = listing.get("support_usage") or (USAGE.get(spec["slug"]) if spec["sale_type"] == "digital" else intake)
        if not usage:
            raise ValueError("该商品尚未配置准确的使用或需求说明：" + spec["slug"])
        profiles.append({"item_id": item_id, "name": spec["name"], "about": about,
                         "usage": usage, "sale_type": spec["sale_type"], "intake": intake, "product": product})
    for item_id in ORANGEBOOKS + (INSTALLER, RESUME) + RESUME_SERVICES:
        if only_item and item_id != only_item:
            continue
        product = store.get("product", product_key(account, item_id))
        if not product:
            continue
        if product.get("account") != account:
            raise ValueError("旧商品账号不一致")
        if item_id in ORANGEBOOKS:
            about = "DeepSeek Harness 橙皮书电子阅读资料与实测记录，涉及启动运行、系统提示词、会话日志、工具创建和扩展源码拆解。不包含一键安装软件或远程服务。"
            usage = "付款后获取夸克分享，下载资料按阅读说明查看；内容对应书中实测的版本与环境。"
        elif item_id == RESUME:
            about = "六书简历知识库按所选规格提供：资料阅读版、AI 知识库成品版、Agent 接入工具版。不含人工代写、录用承诺、模型账号或 API 额度。"
            usage = "阅读版用 EPUB 阅读工具；知识库版用电脑浏览器；Agent 工具版按 Windows 说明自行配置。离线阅读和搜索不需要 API，模型问答或编译需自备模型服务。"
        elif item_id in RESUME_SERVICES:
            about = "针对真实简历或项目材料提供诊断与表达建议，包含问题清单、修改方向和证据整理；按所购档位确认具体范围。不含编造经历、代投或录用保证。"
            usage = RESUME_INTAKE
        else:
            about = "DSH 一键安装包或人工配置服务，按“服务版本”规格交付。一键安装包适用于 Windows 10/11，使用 GitHub 下载；人工安装、安装加 3 个插件及插件定制需沟通需求。模型/API 费用不包含。"
            usage = INSTALLER_INTAKE
        profiles.append({"item_id": item_id, "name": product["title"], "about": about, "usage": usage,
                         "sale_type": "mixed" if item_id == INSTALLER else "service" if item_id in RESUME_SERVICES else "digital",
                         "intake": RESUME_INTAKE if item_id in RESUME_SERVICES else None, "product": product})
    return profiles


def _replies(profile):
    shipping = SERVICE_SHIPPING if profile["sale_type"] == "service" else DIGITAL_SHIPPING
    if profile["sale_type"] == "mixed":
        shipping = "一键安装包规格在付款核实后自动发送 GitHub 下载信息；其余人工规格付款后先发送需求清单，完成服务后才确认发货。"
    groups = [
        (("你好", "在吗", "有吗", "内容", "包含", "目录"), profile["about"]),
        (("怎么用", "使用", "格式", "手机", "电脑"), profile["usage"]),
        (("发货", "多久", "下载", "网盘"), shipping),
        (("价格", "多少钱", "便宜", "优惠"), "价格以当前商品页所选规格为准。定制或超出范围的需求需先确认报价；自动答复不会代卖家承诺优惠或工期。"),
        (("未收到", "链接失效", "打不开", "解压失败", "售后", "退款"), AFTER_SALE),
        (("定制", "代写"), profile.get("intake") or profile["usage"]),
    ]
    return {keyword: reply for aliases, reply in groups for keyword in aliases}


def _fallback(profile):
    return "【自动答复】" + profile["about"] + "\n" + (profile.get("intake") or "可发送“怎么用”“发货”或具体问题。需要人工核对的内容请留在当前会话，卖家查看后回复。")


def configure_all(store: Store, account: str) -> dict:
    profiles = _profiles(store, account)
    changes = []
    staged = {}

    def put(kind, key, value, *, preserve=False):
        before = store.get(kind, key)
        if preserve and before is not None:
            return
        if before != value:
            staged[(kind, key)] = value
            changes.append({"kind": kind, "key": key, "before": before, "after": value})

    existing_keywords = {(str(r.get("item_id")), str(r.get("keyword"))) for r in store.rows("keyword", account)}
    for profile in profiles:
        item_id, product = profile["item_id"], profile["product"]
        for keyword, reply in _replies(profile).items():
            if (item_id, keyword) in existing_keywords:
                continue
            key = "support:" + product_key(account, item_id) + ":" + hashlib.sha256(keyword.encode()).hexdigest()[:12]
            put("keyword", key, {"id": key, "cookie_id": account, "item_id": item_id,
                "keyword": keyword, "reply": reply, "type": "text", "enabled": True, "activated_at": now()}, preserve=True)
        fallback_key = "support:" + product_key(account, item_id) + ":fallback"
        put("keyword", fallback_key, {"id": fallback_key, "cookie_id": account, "item_id": item_id,
            "keyword": "未命中时的首次答复", "match_mode": "fallback", "reply":
            _fallback(profile),
            "type": "text", "enabled": True, "activated_at": now()}, preserve=True)
        put("product", product_key(account, item_id), {**product, "managed": True})
        # Replace fragile legacy title matching with the already verified item identity.
        for rule in store.rows("delivery_rule", account):
            if not rule.get("item_id") and rule.get("keyword") == product.get("title"):
                clean = {k: v for k, v in rule.items() if not k.startswith("_")}
                put("delivery_rule", rule["_key"], {**clean, "item_id": item_id})
        specs = SERVICE_SPECS if item_id == INSTALLER else ("",) if profile["sale_type"] == "service" else ()
        for spec in specs:
            key = "intake:" + product_key(account, item_id) + ":" + (hashlib.sha256(spec.encode()).hexdigest()[:10] if spec else "base")
            message = "已核实本订单付款，感谢购买。\n" + (INSTALLER_INTAKE if spec else profile["intake"]) + "\n这是自动需求清单，服务尚未完成；实际交付与验收由卖家另行确认。"
            put("card", key, {"id": key, "account": account, "name": (spec or profile["name"]) + " · 付款后需求收集",
                "type": "text", "enabled": True, "fulfillment": "service_intake", "is_multi_spec": bool(spec),
                "spec_name": "服务版本" if spec else "", "spec_value": spec, "text_content": message}, preserve=True)
            put("delivery_rule", key, {"id": key, "account": account, "item_id": item_id, "keyword": product["title"],
                "card_id": key, "enabled": True, "delivery_count": 1, "fulfillment": "service_intake",
                "description": "付款后收集需求，不确认平台发货"}, preserve=True)
    receipt = {"id": uuid.uuid4().hex, "account": account, "changed_at": now(), "changes": changes,
               "source_request": "都弄自动发货了吗？没弄得话都弄一下自动发货和自动回复啥的"}
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for change in changes:
            actual = db.execute("SELECT payload FROM records WHERE kind=? AND key=?", (change["kind"], change["key"])).fetchone()
            if (json.loads(actual[0]) if actual else None) != change["before"]:
                raise ValueError("配置在准备期间已变化，未覆盖，请重新核对")
        if changes:
            staged[("support_setup", receipt["id"])] = receipt
        for (kind, key), value in staged.items():
            db.execute("INSERT INTO records(kind,key,account,payload,source,saved_at) VALUES(?,?,?,?,?,?) "
                       "ON CONFLICT(kind,key) DO UPDATE SET account=excluded.account,payload=excluded.payload,source=excluded.source,saved_at=excluded.saved_at",
                       (kind, key, account, json.dumps(value, ensure_ascii=False), "owned_support_setup", now()))
    return {"changed": len(changes), "receipt_id": receipt["id"] if changes else None, "coverage": coverage(store, account)}


def refresh_product(store: Store, account: str, slug: str) -> dict:
    """Refresh generated copy after this item's content update, preserving manual edits and switches."""
    publication = store.get("publication", product_key(account, slug), {})
    if publication.get("state") != "published" or not publication.get("content_current"):
        raise ValueError("先核对该商品的新版线上内容，再更新咨询答复")
    item_id = publication["item_id"]
    profile = next(iter(_profiles(store, account, item_id)), None)
    if not profile:
        raise ValueError("未找到该商品的客服说明")
    replies, previous = _replies(profile), {}
    receipts = store.rows("support_setup", account) + store.rows("support_refresh", account)
    for receipt in sorted(receipts, key=lambda r: r["_saved_at"]):
        for change in receipt.get("changes", []):
            previous[(change["kind"], change["key"])] = change.get("after") or {}
    changes, preserved = [], []
    for kind, field in (("keyword", "reply"), ("card", "text_content")):
        for row in store.rows(kind, account):
            key = row["_key"]
            if kind == "keyword" and str(row.get("item_id")) == item_id and key.startswith("support:" + product_key(account, item_id) + ":"):
                text = _fallback(profile) if row.get("match_mode") == "fallback" else replies.get(row.get("keyword"))
            elif kind == "card" and profile["sale_type"] == "service" and key == "intake:" + product_key(account, item_id) + ":base":
                text = "已核实本订单付款，感谢购买。\n" + profile["intake"] + "\n这是自动需求清单，服务尚未完成；实际交付与验收由卖家另行确认。"
            else:
                continue
            if not text or row.get(field) == text:
                continue
            if row.get(field) != previous.get((kind, key), {}).get(field):
                preserved.append(key)
                continue
            before = {k: v for k, v in row.items() if not k.startswith("_")}
            changes.append({"kind": kind, "key": key, "before": before, "after": {**before, field: text}})
    receipt = {"id": uuid.uuid4().hex, "account": account, "item_id": item_id, "content_id": publication["content_current"]["id"],
               "changed_at": now(), "changes": changes, "preserved_custom_replies": preserved}
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for change in changes:
            found = db.execute("SELECT payload FROM records WHERE kind=? AND key=?", (change["kind"], change["key"])).fetchone()
            if not found or json.loads(found[0]) != change["before"]:
                raise ValueError("客服配置在核对期间改变，未覆盖")
            db.execute("UPDATE records SET payload=?,source=?,saved_at=? WHERE kind=? AND key=?",
                       (json.dumps(change["after"], ensure_ascii=False), "owned_support_refresh", now(), change["kind"], change["key"]))
        db.execute("INSERT INTO records(kind,key,account,payload,source,saved_at) VALUES(?,?,?,?,?,?)",
                   ("support_refresh", receipt["id"], account, json.dumps(receipt, ensure_ascii=False), "owned_support_refresh", now()))
    return {"changed": len(changes), "preserved_custom_replies": preserved, "receipt_id": receipt["id"]}


def coverage(store: Store, account: str) -> list[dict]:
    keywords = store.rows("keyword", account)
    rules = store.rows("delivery_rule", account)
    result = []
    for product in store.rows("product", account):
        item_id = str(product["item_id"])
        matching = [r for r in rules if r.get("enabled") not in (False, 0) and
                    (str(r.get("item_id")) == item_id or (not r.get("item_id") and r.get("keyword") == product.get("title")))]
        actions = []
        for rule in matching:
            card = store.get("card", str(rule.get("card_id")), {})
            if card.get("enabled") in (False, 0) or not card.get("text_content"):
                continue
            mode = rule.get("fulfillment", "delivery")
            if mode != card.get("fulfillment", "delivery"):
                continue
            actions.append({"mode": mode, "spec": card.get("spec_value") or "单规格"})
        replies = [r for r in keywords if str(r.get("item_id")) == item_id and r.get("enabled") not in (False, 0)]
        result.append({"item_id": item_id, "title": product.get("title"), "status": product.get("status"), "managed": bool(product.get("managed")),
            "keyword_count": sum(r.get("match_mode") != "fallback" for r in replies),
            "fallback": any(r.get("match_mode") == "fallback" for r in replies), "actions": actions})
    return result
