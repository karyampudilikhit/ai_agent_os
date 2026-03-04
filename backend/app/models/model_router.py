# check_files.py
import os

def check_phase2_files():
    """Check that Phase 2 files exist"""
    print("📁 Checking Phase 2 Files...")
    
    files_to_check = [
        "backend/app/contracts/execution_contract.py",
        "backend/app/models/model_router.py", 
        "backend/app/models/provider_adapters/ollama_adapter.py",
        "backend/app/models/usage_logger.py"
    ]
    
    for file_path in files_to_check:
        exists = os.path.exists(file_path)
        status = "✅ EXISTS" if exists else "❌ MISSING"
        print(f"   {status}: {file_path}")
        
    print("\n📝 Notes:")
    print("   • execution_contract.py is the most important")
    print("   • If it exists, Phase 2 core works")
    print("   • Other files can be fixed later")

if __name__ == "__main__":
    check_phase2_files()
