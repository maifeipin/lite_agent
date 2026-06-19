import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
import uuid
import concurrent.futures
from pydantic import BaseModel, Field
from typing import List, Dict, Literal, Optional
from skill_engine import skill
from config_loader import load_config
from model_router import ModelRouter

# =========================================================
# 1. Schema 定义 (稳定输出协议)
# =========================================================
class CriteriaScore(BaseModel):
    name: str = Field(description="维度名称")
    score: int = Field(ge=0, le=100, description="该维度的得分(0-100)")
    rationale: str = Field(description="打分理由")

class BaseScores(BaseModel):
    profitability: int = Field(ge=0, le=100)
    execution: int = Field(ge=0, le=100)
    timeliness: int = Field(ge=0, le=100)

class DecisionResult(BaseModel):
    decision_type: str = Field(description="结论类型方向，建议使用标准词汇，例如：'值得执行', '高风险放弃', '暂缓观察'")
    base_scores: BaseScores = Field(description="固定维度得分")
    extra_scores: List[CriteriaScore] = Field(description="额外维度的得分", default_factory=list)
    model_reported_overall_score: int = Field(ge=0, le=100, description="你(模型)主观认为的综合得分")
    confidence_score: int = Field(ge=0, le=100, description="你对本次打分的整体置信度")
    reasoning: str = Field(description="核心论证逻辑")
    evidence_gaps: List[str] = Field(description="缺失的证据或前提假设", default_factory=list)
    action_item: str = Field(description="下一步建议的行动指令")

class PeerReview(BaseModel):
    reviewed_model_id: str
    valid_points: List[str] = Field(description="对方有道理的观点")
    missing_evidence: List[str] = Field(description="对方缺失的证据")
    logic_errors: List[str] = Field(description="对方推理错误或事实遗漏的地方")
    disagreement_type: Literal["fact", "weight", "preference", "uncertainty"] = Field(description="分歧类型")
    suggested_score_delta: int = Field(ge=-20, le=20, description="建议总分调整量")
    should_escalate_to_human: bool = Field(description="是否因为分歧严重而必须交由人类裁决")

# =========================================================
# 2. 核心调度与实现
# =========================================================
def _get_models_from_config() -> List[str]:
    cfg = load_config()
    return cfg.get("committee", {}).get("models", ["flash", "gemini-pro"])

def _get_task_profile(task_type: str) -> dict:
    cfg = load_config()
    profiles = cfg.get("committee", {}).get("task_profiles", {})
    return profiles.get(task_type, {
        "base_weights": {
            "profitability": 0.4, 
            "execution": 0.3, 
            "timeliness": 0.3
        },
        "extra_criteria": [],
        "description": "通用决策评估"
    })

def _build_decision_brief(router: ModelRouter, topic: str) -> str:
    # MVP: 如果文本超过 8000 字符，调用最快模型做统一摘要
    if len(topic) < 8000:
        return topic
    prompt = f"请提炼以下长文本的核心事实基线，用于后续公平决策投票。请包含：Key Facts, Quantitative Signals, Known Uncertainties, Source Coverage。\n\n{topic[:30000]}"
    try:
        client = router.get_client("flash")
        provider = router.get_provider("flash")
        model_id = router.models_cfg.get("flash", {}).get("model", "flash")
        
        if provider == "gemini":
            res = client.models.generate_content(model=model_id, contents=prompt)
            return res.text
        else:
            response = client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.choices[0].message.content
    except Exception as e:
        return topic[:8000] + "\n...[Truncated and Brief Failed]"

def _call_model(router: ModelRouter, model_name: str, prompt: str, schema_cls) -> str:
    """并发调用大模型，并强制输出指定 Schema"""
    client = router.get_client(model_name)
    provider = router.get_provider(model_name)
    if not client:
        return '{"error": "Model client not found"}'
        
    sys_prompt = f"You are an expert AI committee judge. You must output valid JSON matching exactly this schema:\n{schema_cls.model_json_schema()}"
    
    try:
        model_id = router.models_cfg.get(model_name, {}).get("model", model_name)
        if provider == "gemini":
            res = client.models.generate_content(
                model=model_id,
                contents=sys_prompt + "\n\n" + prompt
            )
            return res.text
        else:
            response = client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                timeout=45
            )
            return response.choices[0].message.content
    except Exception as e:
        return f'{{"error": "{str(e)}" }}'

def _compute_weighted_score(res: DecisionResult, profile: dict) -> float:
    base_weights = profile.get("base_weights", {})
    extra_criteria = profile.get("extra_criteria", [])
    
    total_score = 0.0
    total_weight = 0.0
    
    # Base scores
    for k, v in dict(res.base_scores).items():
        weight = base_weights.get(k, 0)
        total_score += v * weight
        total_weight += weight
        
    # Extra scores
    extra_weight_map = {c["name"]: c.get("weight", 0) for c in extra_criteria}
    for criteria in res.extra_scores:
        weight = extra_weight_map.get(criteria.name, 0)
        total_score += criteria.score * weight
        total_weight += weight
        
    if total_weight > 0:
        return total_score / total_weight
    return float(res.model_reported_overall_score)

def _calculate_variance(scores: List[float]) -> float:
    if len(scores) < 2: return 0.0
    mean = sum(scores) / len(scores)
    variance = sum((x - mean) ** 2 for x in scores) / len(scores)
    return variance ** 0.5

@skill(
    name='ops_decision',
    description='多模型评判委员会引擎。用于对复杂报告、需求或商业行动进行多专家并行评分与盲审。',
    params={
        'task_type': {'type': 'string', 'description': '评估的任务类型，如果没有指定请传 default', 'default': 'default'},
        'topic': {'type': 'string', 'description': '需要交给委员会进行评估的议题数据'}
    }
)
def ops_decision(task_type: str, topic: str) -> str:
    router = ModelRouter(load_config())
    models = _get_models_from_config()
    profile = _get_task_profile(task_type)
    run_id = str(uuid.uuid4())[:8]
    
    # 步骤一：预处理生成 Decision Brief
    brief = _build_decision_brief(router, topic)
    prompt = f"任务配置指南与维度定义: {json.dumps(profile, ensure_ascii=False)}\n\n请严格按照 Schema 对以下简报事实进行打分和评价：\n\n{brief}"
    
    results = {}
    computed_scores = []
    
    # 步骤二：并发调用模型
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as executor:
        future_to_model = {executor.submit(_call_model, router, m, prompt, DecisionResult): m for m in models}
        for future in concurrent.futures.as_completed(future_to_model):
            m = future_to_model[future]
            res_str = future.result()
            try:
                start = res_str.find('{')
                end = res_str.rfind('}') + 1
                if start != -1 and end != 0:
                    res_str = res_str[start:end]
                
                res_obj = DecisionResult.model_validate_json(res_str)
                # 计算引擎加权得分
                c_score = _compute_weighted_score(res_obj, profile)
                results[m] = {"res": res_obj, "computed_score": c_score}
                computed_scores.append(c_score)
            except Exception as e:
                results[m] = {"error": f"解析失败 ({e}) | Raw: {res_str[:150]}"}
                
    # 审计存档
    audit_data = {
        "run_id": run_id,
        "task_type": task_type,
        "profile": profile,
        "brief": brief,
        "results": {k: (v["res"].model_dump() if "res" in v else v) for k, v in results.items()}
    }
    project_root = load_config().get("project_root", "/tmp")
    data_dir = os.path.join(project_root, "data", "committee")
    os.makedirs(data_dir, exist_ok=True)
    audit_path = os.path.join(data_dir, f"audit_{run_id}.json")
    with open(audit_path, "w", encoding="utf-8") as f:
        json.dump(audit_data, f, ensure_ascii=False, indent=2)
        
    if not computed_scores:
        return f"🚨 所有模型评估失败。详情见审计: {audit_path}"
        
    std_dev = _calculate_variance(computed_scores)
    
    # 检测决策方向冲突
    valid_decisions = [v["res"].decision_type for v in results.values() if "res" in v]
    direction_conflict = len(set(valid_decisions)) > 1
    
    output = [f"📊 委员会评审完毕 [RunID: {run_id}] | 得分方差: {std_dev:.2f}", ""]
    
    for m, data in results.items():
        if "res" in data:
            res = data["res"]
            score = data["computed_score"]
            output.append(f"### 🤖 评委 [{m}]: `引擎核算分: {score:.1f}` (置信度: {res.confidence_score}) | 结论: `{res.decision_type}`")
            output.append(f"- **核心理由**: {res.reasoning}")
            if res.evidence_gaps:
                output.append(f"- **证据缺失**: {', '.join(res.evidence_gaps)}")
            output.append(f"- **行动项**: {res.action_item}\n")
        else:
            output.append(f"### 🤖 评委 [{m}]: 评估失败")
            output.append(f"- {data['error']}\n")
            
    output.append("---")
    output.append(f"📂 机器审计日志已归档: `{audit_path}`")
    
    # 步骤四：分歧判断与处理机制
    if std_dev > 25 or direction_conflict:
        output.append("\n🚨 **高级警报：模型间存在严重分歧**")
        if direction_conflict:
            output.append("> 原因：各模型的 `decision_type` 核心结论方向不一致。")
        else:
            output.append("> 原因：总分方差大于 25。")
        output.append("> 已熔断自动判定，请人类介入查阅上方 reasoning。")
    elif std_dev > 12:
        output.append("\n⚠️ **警告：模型中度分歧 (方差>12)**")
        output.append("> (V2将触发 PeerReview 进行结构化纠错)")
    else:
        output.append("\n✅ **委员会达成高度共识**")
        output.append("> 可以信任并执行评委们给出的下一步 Action Item。")
        
    return "\n".join(output)
