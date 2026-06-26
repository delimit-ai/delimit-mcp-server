import pytest
import os
import tempfile
from gateway.ai.governance_hardening import StateValidator

def test_state_validator_file_system():
    validator = StateValidator()
    
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = os.path.join(tmpdir, "test.txt")
        with open(filepath, "w") as f:
            f.write("initial")
            
        args = {"path": filepath}
        
        # First call always True
        assert validator.validate_state_change("file_system", args, tmpdir) is True
        
        # Second call without change -> False
        assert validator.validate_state_change("file_system", args, tmpdir) is False
        
        # Change file content
        with open(filepath, "w") as f:
            f.write("changed")
            
        # Call after change -> True
        assert validator.validate_state_change("file_system", args, tmpdir) is True

def test_state_validator_command():
    validator = StateValidator()
    args = {"command": "ls -l"}
    
    # First call True
    assert validator.validate_state_change("command", args) is True
    
    # Second call without change -> False
    assert validator.validate_state_change("command", args) is False
