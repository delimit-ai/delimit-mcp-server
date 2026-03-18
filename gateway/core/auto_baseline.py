"""
Auto-Baseline Mode for Gradual Adoption
Enables teams to start governance without failing on existing issues
"""

import json
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List

from schemas.evidence import TaskEvidence, Decision, Violation


class AutoBaseline:
    """
    Manages automatic baseline generation and comparison.
    Allows gradual adoption by only flagging NEW violations.
    """
    
    def __init__(self, baseline_dir: Optional[Path] = None):
        """
        Initialize auto-baseline manager.
        
        Args:
            baseline_dir: Directory to store baselines (default: ~/.delimit/baselines)
        """
        self.baseline_dir = baseline_dir or (Path.home() / ".delimit" / "baselines")
        self.baseline_dir.mkdir(parents=True, exist_ok=True)
    
    def get_baseline_path(self, file_path: str, task: str) -> Path:
        """
        Get the baseline file path for a given source file.
        
        Args:
            file_path: Path to the source file
            task: Task name (validate-api, check-policy, etc)
            
        Returns:
            Path to baseline file
        """
        file_hash = hashlib.md5(file_path.encode()).hexdigest()[:8]
        file_name = Path(file_path).stem
        baseline_name = f"{file_name}_{task}_{file_hash}.baseline.json"
        return self.baseline_dir / baseline_name
    
    def load_baseline(self, file_path: str, task: str) -> Optional[Dict[str, Any]]:
        """
        Load existing baseline if it exists.
        
        Args:
            file_path: Path to the source file
            task: Task name
            
        Returns:
            Baseline data or None if not found
        """
        baseline_path = self.get_baseline_path(file_path, task)
        
        if baseline_path.exists():
            with baseline_path.open('r') as f:
                return json.load(f)
        
        return None
    
    def save_baseline(self, file_path: str, task: str, evidence: TaskEvidence) -> Path:
        """
        Save current results as baseline.
        
        Args:
            file_path: Path to the source file
            task: Task name
            evidence: Task evidence to baseline
            
        Returns:
            Path to saved baseline
        """
        baseline_path = self.get_baseline_path(file_path, task)
        
        baseline_data = {
            "timestamp": datetime.now().isoformat(),
            "file": file_path,
            "task": task,
            "violations": [v.model_dump(mode='json') for v in evidence.violations],
            "metrics": evidence.metrics,
            "checksum": self._calculate_file_checksum(file_path)
        }
        
        with baseline_path.open('w') as f:
            json.dump(baseline_data, f, indent=2)
        
        return baseline_path
    
    def filter_new_violations(self, 
                            current_evidence: TaskEvidence,
                            baseline_data: Dict[str, Any]) -> TaskEvidence:
        """
        Filter violations to only show NEW issues not in baseline.
        
        Args:
            current_evidence: Current task evidence
            baseline_data: Baseline data to compare against
            
        Returns:
            Modified evidence with only new violations
        """
        baseline_violations = baseline_data.get("violations", [])
        
        # Create set of baseline violation signatures
        baseline_sigs = set()
        for v in baseline_violations:
            sig = f"{v.get('rule')}:{v.get('path', '')}:{v.get('message', '')}"
            baseline_sigs.add(sig)
        
        # Filter to only new violations
        new_violations = []
        baselined_count = 0
        
        for violation in current_evidence.violations:
            sig = f"{violation.rule}:{violation.path or ''}:{violation.message}"
            if sig not in baseline_sigs:
                new_violations.append(violation)
            else:
                baselined_count += 1
        
        # Update evidence
        current_evidence.violations = new_violations
        
        # Adjust decision based on new violations only
        if len(new_violations) == 0:
            current_evidence.decision = Decision.PASS
            current_evidence.exit_code = 0
            current_evidence.summary = f"No new violations found ({baselined_count} baselined)"
        else:
            # Keep original decision for new violations
            current_evidence.summary = f"{current_evidence.summary} ({baselined_count} baselined)"
        
        # Add baseline info to metrics
        current_evidence.metrics["baselined_violations"] = baselined_count
        current_evidence.metrics["new_violations"] = len(new_violations)
        current_evidence.metrics["baseline_applied"] = True
        
        return current_evidence
    
    def apply_auto_baseline(self,
                           file_path: str,
                           task: str,
                           evidence: TaskEvidence,
                           create_if_missing: bool = True) -> TaskEvidence:
        """
        Apply auto-baseline logic to task evidence.
        
        Args:
            file_path: Path to the source file
            task: Task name
            evidence: Task evidence to process
            create_if_missing: Create baseline if it doesn't exist
            
        Returns:
            Modified evidence with baseline applied
        """
        baseline = self.load_baseline(file_path, task)
        
        if baseline is None:
            if create_if_missing and evidence.violations:
                # First run - create baseline
                baseline_path = self.save_baseline(file_path, task, evidence)
                
                # On first baseline, pass with warning
                evidence.decision = Decision.WARN
                evidence.exit_code = 0
                evidence.summary = f"Baseline created with {len(evidence.violations)} violations"
                evidence.metrics["baseline_created"] = True
                evidence.metrics["baseline_path"] = str(baseline_path)
            else:
                # No baseline and no violations - pass normally
                evidence.metrics["baseline_applied"] = False
        else:
            # Apply baseline filtering
            evidence = self.filter_new_violations(evidence, baseline)
        
        return evidence
    
    def _calculate_file_checksum(self, file_path: str) -> str:
        """Calculate checksum of file for change detection."""
        try:
            with open(file_path, 'rb') as f:
                return hashlib.sha256(f.read()).hexdigest()
        except:
            return ""
    
    def update_baseline(self,
                       file_path: str,
                       task: str,
                       evidence: TaskEvidence,
                       threshold: float = 0.8) -> bool:
        """
        Update baseline if improvement threshold is met.
        
        Args:
            file_path: Path to the source file
            task: Task name
            evidence: Current task evidence
            threshold: Improvement threshold (0.8 = 20% reduction required)
            
        Returns:
            True if baseline was updated
        """
        baseline = self.load_baseline(file_path, task)
        
        if baseline is None:
            # No existing baseline
            self.save_baseline(file_path, task, evidence)
            return True
        
        # Check if violations have improved enough
        baseline_count = len(baseline.get("violations", []))
        current_count = len(evidence.violations)
        
        if current_count <= baseline_count * threshold:
            # Significant improvement - update baseline
            self.save_baseline(file_path, task, evidence)
            return True
        
        return False
    
    def get_baseline_status(self) -> Dict[str, Any]:
        """
        Get status of all baselines.
        
        Returns:
            Status information about baselines
        """
        baselines = list(self.baseline_dir.glob("*.baseline.json"))
        
        status = {
            "baseline_dir": str(self.baseline_dir),
            "total_baselines": len(baselines),
            "baselines": []
        }
        
        for baseline_file in baselines:
            with baseline_file.open('r') as f:
                data = json.load(f)
                status["baselines"].append({
                    "file": data.get("file"),
                    "task": data.get("task"),
                    "timestamp": data.get("timestamp"),
                    "violations_count": len(data.get("violations", [])),
                    "path": str(baseline_file)
                })
        
        return status
    
    def clear_baseline(self, file_path: Optional[str] = None, task: Optional[str] = None) -> int:
        """
        Clear baselines.
        
        Args:
            file_path: Specific file to clear baseline for (optional)
            task: Specific task to clear baseline for (optional)
            
        Returns:
            Number of baselines cleared
        """
        count = 0
        
        if file_path and task:
            # Clear specific baseline
            baseline_path = self.get_baseline_path(file_path, task)
            if baseline_path.exists():
                baseline_path.unlink()
                count = 1
        else:
            # Clear all baselines
            for baseline_file in self.baseline_dir.glob("*.baseline.json"):
                baseline_file.unlink()
                count += 1
        
        return count


# Convenience functions
def apply_auto_baseline(evidence: TaskEvidence,
                       file_path: str,
                       task: str,
                       enabled: bool = False) -> TaskEvidence:
    """
    Apply auto-baseline to evidence if enabled.
    
    Args:
        evidence: Task evidence
        file_path: Source file path
        task: Task name
        enabled: Whether auto-baseline is enabled
        
    Returns:
        Potentially modified evidence
    """
    if not enabled:
        return evidence
    
    baseline_manager = AutoBaseline()
    return baseline_manager.apply_auto_baseline(file_path, task, evidence)