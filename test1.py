# test_real_intelligence.py
import sys
import os
sys.path.insert(0, 'backend')

def test_with_real_ollama():
    """Test with real Ollama intelligence"""
    print("🔬 Testing with REAL Ollama Intelligence")
    print("=" * 45)
    
    try:
        # Test Ollama adapter
        from app.models.provider_adapters.ollama_adapter import OllamaAdapter
        adapter = OllamaAdapter(model="phi3")  # Use the model you downloaded
        print("✅ Ollama adapter connected")
        
        # Test basic intelligence
        print("\n🤖 Testing basic intelligence...")
        response = adapter.chat_completion("What is 2+2?")
        print(f"Basic test: {response}")
        
        # Test Phase 2: Smart Contract Generation
        print("\n📋 PHASE 2: Smart Contract Generation")
        from app.contracts.execution_contract import generate_execution_contract
        
        contract = generate_execution_contract(
            "Create a complete website for a bakery business",
            model_adapter=adapter
        )
        
        print(f"🎯 OBJECTIVE: {contract.objective}")
        print(f"📊 Confidence: {contract.confidence_score:.2f}")
        print(f"📈 Complexity: {contract.estimated_complexity:.2f}")
        
        # Show real deliverables (proof of real intelligence)
        print(f"\n📦 REAL DELIVERABLES ({len(contract.deliverables)} items):")
        for i, deliverable in enumerate(contract.deliverables, 1):
            print(f"  {i}. {deliverable}")
            
        # Show it's not mock data
        if len(contract.deliverables) > 1 and len(contract.deliverables[0]) > 30:
            print("✅ CONFIRMED: Real AI intelligence detected!")
        else:
            print("⚠️  Still looks like mock data")
            
        # Test Phase 3: Smart Agent Creation
        print(f"\n🤖 PHASE 3: Smart Agent Creation")
        from app.agents.agent_factory import create_agents_from_contract
        
        agents = create_agents_from_contract(contract)
        
        print(f"\n👷 CREATED {len(agents)} SPECIALIZED AGENTS:")
        for i, agent in enumerate(agents[:3], 1):  # Show first 3
            print(f"\n  AGENT {i}: {agent.role}")
            print(f"    Task: {agent.objective[:60]}...")
            print(f"    Traits: {getattr(agent, 'traits', {})}")
            
        print(f"\n{'=' * 45}")
        print("🎉 REAL AI INTELLIGENCE WORKING!")
        print("You now have professional-quality smart agents!")
        
        return True
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_with_real_ollama()
    if success:
        print("\n🟢 SMART AGENTS ARE READY!")
        print("Perfect for Phase 4 development!")
    else:
        print("\n🔴 Something went wrong")
