# test_now.py
import sys
import os
sys.path.insert(0, 'backend')

# This will work immediately - no setup needed!
from app.contracts.execution_contract import generate_execution_contract

contract = generate_execution_contract("Test objective")
print("✅ Works without any setup!")
print(f"Contract: {contract.objective}")
