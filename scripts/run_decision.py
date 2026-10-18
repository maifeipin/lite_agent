import sys
import os

# 将项目根目录加入 sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from lite_agent.skills.ops_decision import ops_decision

def main():
    if len(sys.argv) < 2:
        print("💡 使用指南:")
        print("  1. 传入文本评估:  python run_decision.py \"议题描述文本...\" [任务类型]")
        print("  2. 读取文件评估:  python run_decision.py path/to/topic.txt [任务类型]")
        print("  *(任务类型可选: default, trend_analysis, price_comparison)*")
        sys.exit(1)
        
    arg = sys.argv[1]
    task_type = sys.argv[2] if len(sys.argv) > 2 else "default"
    
    # 如果传入的是文件路径，则读取文件内容作为议题
    if os.path.exists(arg):
        with open(arg, 'r', encoding='utf-8') as f:
            topic = f.read()
        print(f"📖 已从文件读取议题: {os.path.abspath(arg)}")
    else:
        topic = arg
        
    print(f"🚀 正在启动多模型评判委员会 [评估类型: {task_type}]，请稍候...\n")
    result = ops_decision(task_type=task_type, topic=topic)
    
    print("\n" + "="*60)
    print(result)
    print("="*60)

if __name__ == "__main__":
    main()
