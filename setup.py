import os

def create_ai_workforce_os_structure(base_path="."):
    """Create the complete directory and file structure"""
    
    # Define the complete structure
    structure = {
        "backend": {
            "app": {
                "main.py": None,
                "config.yaml": None,
                "api": {
                    "routes.py": None,
                    "schemas.py": None
                },
                "orchestrator": {
                    "pipeline_controller.py": None,
                    "execution_engine.py": None,
                    "adaptive_supervisor.py": None,
                    "dependency_validator.py": None,
                    "state_manager.py": None
                },
                "contracts": {
                    "execution_contract.py": None
                },
                "agents": {
                    "agent_factory.py": None,
                    "agent_schema.py": None,
                    "agent_executor.py": None,
                    "agent_validator.py": None
                },
                "traits": {
                    "trait_config.py": None,
                    "trait_validator.py": None,
                    "trait_mapper.py": None
                },
                "critique": {
                    "critique_agent.py": None,
                    "confidence_evaluator.py": None
                },
                "models": {
                    "model_router.py": None,
                    "usage_logger.py": None,
                    "provider_adapters": {
                        "openai_adapter.py": None,
                        "anthropic_adapter.py": None,
                        "gemini_adapter.py": None
                    }
                },
                "workflows": {
                    "workflow_manager.py": None,
                    "workflow_versioning.py": None,
                    "scheduler.py": None
                },
                "tools": {
                    "tool_registry.py": None,
                    "api_tool_handler.py": None,
                    "browser_automation.py": None,
                    "tool_guardrails.py": None
                },
                "memory": {
                    "memory_manager.py": None,
                    "mistake_repository.py": None,
                    "embedding_store.py": None
                },
                "safety": {
                    "recursion_guard.py": None,
                    "constraints_enforcer.py": None,
                    "input_output_validator.py": None
                },
                "logging": {
                    "audit_trail.py": None,
                    "visual_graph_generator.py": None,
                    "event_dispatcher.py": None
                },
                "state": {
                    "execution_state.py": None
                },
                "utils": {
                    "cost_calculator.py": None,
                    "error_handling.py": None,
                    "json_utils.py": None
                }
            },
            "tests": {},
            "requirements.txt": None,
            "Dockerfile": None
        },
        "frontend": {
            "public": {
                "index.html": None
            },
            "src": {
                "App.jsx": None,
                "index.jsx": None,
                "components": {
                    "layout": {
                        "TopBar.jsx": None,
                        "Sidebar.jsx": None,
                        "CommandBar.jsx": None
                    },
                    "execution": {
                        "ExecutionGraph.jsx": None,
                        "ExecutionTimeline.jsx": None,
                        "AgentStatus.jsx": None
                    },
                    "contracts": {
                        "ContractEditor.jsx": None
                    },
                    "agents": {
                        "AgentViewer.jsx": None,
                        "TraitInspector.jsx": None
                    },
                    "critique": {
                        "CritiqueReport.jsx": None
                    },
                    "tools": {
                        "ToolMonitor.jsx": None
                    },
                    "output": {
                        "OutputViewer.jsx": None
                    }
                },
                "pages": {
                    "Dashboard.jsx": None,
                    "NewTask.jsx": None,
                    "TaskHistory.jsx": None,
                    "Settings.jsx": None
                },
                "services": {
                    "api.js": None,
                    "eventBus.js": None
                },
                "state": {
                    "workflowStore.js": None,
                    "executionStore.js": None,
                    "traitStore.js": None
                },
                "styles": {
                    "tailwind.config.js": None
                }
            },
            "package.json": None,
            "vite.config.js": None
        },
        "docs": {},
        "README.md": None,
        "docker-compose.yml": None
    }
    
    def create_structure(path, struct):
        """Recursively create directories and files"""
        for name, content in struct.items():
            current_path = os.path.join(path, name)
            if content is None:
                # Create file
                with open(current_path, 'w') as f:
                    pass  # Create empty file
            elif isinstance(content, dict):
                # Create directory and recurse
                os.makedirs(current_path, exist_ok=True)
                create_structure(current_path, content)
            else:
                # Create file with content
                os.makedirs(os.path.dirname(current_path), exist_ok=True)
                with open(current_path, 'w') as f:
                    f.write(content)
    
    # Create the base directory
    os.makedirs(base_path, exist_ok=True)
    
    # Create the entire structure
    create_structure(base_path, structure)
    
    print(f"✅ AI Workforce OS structure created at: {base_path}")

# Run the function to create the structure
if __name__ == "__main__":
    create_ai_workforce_os_structure()
