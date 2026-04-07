#!/usr/bin/env python3
"""
Ralph Loop Orchestrator (harness.py) — Template

자율 개발 CLI. Claude Code를 Phase별로 호출하여 자율 개발한다.
보조 에이전트(Codex)는 Claude가 반복 에러에 막힐 때 자동 호출.

사용법:
    python harness.py                          # 전체 실행
    python harness.py --phase 4                # Phase 4부터 시작
    python harness.py --phase 4 --end-phase 6  # Phase 4~6만 실행
    python harness.py --dry-run                 # 실행 없이 계획만 출력
    python harness.py --model sonnet            # Sonnet으로 실행
    python harness.py --model opus              # Opus로 실행
    python harness.py --skip-verify             # 검증 스킵 (디버깅용)
    python harness.py --interactive             # 대화형 모드 (--print 대신)
"""

import subprocess
import sys
import os
import json
import time
import re
import argparse
import logging
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─── 설정 (프로젝트별 커스텀) ───────────────────────────────

PROJECT_DIR = Path(__file__).parent
TODO_FILE = PROJECT_DIR / "TODO.md"
CLAUDE_MD = PROJECT_DIR / "CLAUDE.md"
PRD_MD = PROJECT_DIR / "PRD.md"
LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Claude Code CLI 경로 (Windows는 .cmd 확장자 필요)
CLAUDE_CMD = os.environ.get("CLAUDE_CODE_CMD",
    "claude.cmd" if os.name == "nt" else "claude")

# 보조 에이전트 (Codex CLI, 없으면 건너뜀)
CODEX_CMD = os.environ.get("CODEX_CMD", "codex")

# 재시도 설정
MAX_RETRIES_PER_PHASE = 5
RETRY_DELAY_SECONDS = 30
RATE_LIMIT_WAIT_SECONDS = 120
STUCK_THRESHOLD = 3  # 같은 에러 N회 반복 시 보조 에이전트 호출

# ─── TODO: 프로젝트별 수정 필요 ─────────────────────────────

# Phase당 타임아웃 (초)
PHASE_TIMEOUT = {
    1: 1200,   # 20분
    2: 1800,   # 30분
    3: 2400,   # 40분
    # 추가 Phase...
}

# 모델 매핑 (Claude Code CLI --model 값)
MODEL_MAP = {
    "sonnet": "claude-sonnet-4-20250514",
    "opus": "claude-opus-4-20250514",
}

# Phase별 기본 모델 (마지막 통합 Phase는 opus 추천)
PHASE_MODEL = {
    1: "sonnet",
    2: "sonnet",
    3: "sonnet",
    # 추가 Phase...
}

# Phase별 검증 명령어
# TypeScript 프로젝트:
PHASE_VERIFY = {
    1: ["npx tsc --noEmit", "npx eslint src/ --max-warnings 0", "npx vite build"],
    2: ["npx eslint src/ --max-warnings 0"],
    3: ["npx tsc --noEmit", "npx eslint src/ --max-warnings 0", "npx vitest run", "npx vite build"],
    # 추가 Phase...
}
# Python 프로젝트:
# PHASE_VERIFY = {
#     1: ["poetry run ruff check .", "poetry run mypy ."],
#     2: ["poetry run pytest --tb=short"],
# }

# Git 커밋 메시지
COMMIT_MESSAGES = {
    1: "feat(init): project scaffolding",
    2: "test(all): TDD red phase",
    3: "feat(core): core implementation",
    # 추가 Phase...
}

# 완료 신호 접두사 (TODO.md 완료 조건에서 사용)
COMPLETION_SIGNAL = "PROJECT_COMPLETE"

# TODO: 프로젝트별 lint/타입 규칙 가이드 (프롬프트에 포함됨)
# --print 모드의 에이전트는 이전 시도를 기억하지 못하므로, 자주 위반하는 규칙을 여기에 명시
LINT_RULES_GUIDE = """
""".strip()
# 예시 (TypeScript 프로젝트):
# LINT_RULES_GUIDE = """
# - `||` 대신 `??` (nullish coalescing) 사용. boolean 표현식은 예외
# - `_` prefix 변수는 unused 허용
# - .tsx 파일: 함수당 최대 100줄, 파일당 최대 400줄
# - `as` type assertion 최소화 → type guard 또는 Zod parse 사용
# - console.log 금지 → logger 사용
# """.strip()

# TODO: Phase별 특별 규칙 (해당 Phase에만 추가되는 프롬프트)
PHASE_EXTRA_PROMPT: dict[int, str] = {
    # 2: "테스트 코드만 작성한다. 구현은 하지 않는다.",
}

# ─── 로깅 ───────────────────────────────────────────────

def setup_logging() -> logging.Logger:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"ralph_{timestamp}.log"

    logger = logging.getLogger("ralph")
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)

    logger.info(f"Log file: {log_file}")
    return logger

# ─── TODO.md 파싱 ────────────────────────────────────────

def parse_todo() -> dict:
    """TODO.md를 파싱하여 Phase별 완료 상태를 반환"""
    content = TODO_FILE.read_text(encoding="utf-8")
    phases = {}
    current_phase = None

    for line in content.splitlines():
        phase_match = re.match(r"^## Phase (\d+):", line)
        if phase_match:
            current_phase = int(phase_match.group(1))
            phases[current_phase] = {"total": 0, "done": 0, "items": []}
            continue

        if current_phase and re.match(r"^- \[([ x])\]", line):
            checked = line.startswith("- [x]")
            phases[current_phase]["total"] += 1
            if checked:
                phases[current_phase]["done"] += 1
            phases[current_phase]["items"].append({
                "text": line,
                "done": checked
            })

    return phases

def get_next_phase(phases: dict) -> Optional[int]:
    """다음 미완료 Phase 번호를 반환"""
    for phase_num in sorted(phases.keys()):
        phase = phases[phase_num]
        if phase["done"] < phase["total"]:
            return phase_num
    return None

def print_progress(phases: dict, logger: logging.Logger):
    """현재 진행 상황을 출력"""
    logger.info("=" * 60)
    logger.info("Progress")
    logger.info("=" * 60)
    total_done = 0
    total_all = 0
    for phase_num in sorted(phases.keys()):
        p = phases[phase_num]
        total_done += p["done"]
        total_all += p["total"]
        bar = "#" * p["done"] + "." * (p["total"] - p["done"])
        status = "DONE" if p["done"] == p["total"] else "WIP " if p["done"] > 0 else "    "
        logger.info(f"  Phase {phase_num:2d}: {status} [{bar}] {p['done']}/{p['total']}")

    pct = (total_done / total_all * 100) if total_all > 0 else 0
    logger.info(f"\n  Total: {total_done}/{total_all} ({pct:.0f}%)")
    logger.info("=" * 60)

# ─── 검증 ────────────────────────────────────────────────

def run_verification(phase_num: int, logger: logging.Logger) -> tuple[bool, str]:
    """Phase별 검증 명령어를 실행하고 결과를 반환"""
    commands = PHASE_VERIFY.get(phase_num, [])
    if not commands:
        logger.info("  No verification commands — skip")
        return True, ""

    # ESLint auto-fix 실행 (검증 전 trivial 에러 자동 수정)
    if any("eslint" in cmd for cmd in commands):
        logger.info("  ESLint auto-fix...")
        subprocess.run(
            "npx eslint src/ --fix", shell=True, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=120, cwd=str(PROJECT_DIR)
        )

    all_output = []
    for cmd in commands:
        logger.info(f"  Verify: {cmd}")
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=120, cwd=str(PROJECT_DIR)
            )
            output = (result.stdout or "") + (result.stderr or "")
            all_output.append(f"[{cmd}]\n{output}")

            if result.returncode != 0:
                logger.warning(f"  FAIL: {cmd}")
                logger.debug(output[:500])
                return False, "\n".join(all_output)
            else:
                logger.info(f"  PASS: {cmd}")
        except subprocess.TimeoutExpired:
            logger.warning(f"  TIMEOUT: {cmd}")
            return False, f"TIMEOUT: {cmd}"

    return True, "\n".join(all_output)

# ─── Git 자동 커밋 ────────────────────────────────────────

def git_auto_commit(phase_num: int, logger: logging.Logger):
    """Phase 완료 후 자동 git commit"""
    msg = COMMIT_MESSAGES.get(phase_num, f"feat(phase-{phase_num}): phase {phase_num} complete")

    try:
        subprocess.run(["git", "add", "-A"], cwd=str(PROJECT_DIR), capture_output=True,
                       encoding="utf-8", errors="replace")
        result = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(PROJECT_DIR), capture_output=True, text=True,
            encoding="utf-8", errors="replace"
        )
        if result.returncode == 0:
            logger.info(f"  Git commit: {msg}")
        else:
            logger.warning(f"  Git commit failed: {(result.stderr or '')[:200]}")
    except FileNotFoundError:
        logger.warning("  git not found — skip commit")

# ─── Claude Code 실행 ────────────────────────────────────

def build_prompt(phase_num: int, retry_context: str = "") -> str:
    """Phase별 실행 프롬프트 생성"""

    base_prompt = f"""
CLAUDE.md, PRD.md, TODO.md를 읽고 Phase {phase_num}을 구현하라.

필수 프로세스 (항목마다 READ → IMPLEMENT → GATE 사이클):
1. TODO.md에서 Phase {phase_num}의 미완료(`[ ]`) 항목을 확인
2. PRD.md에서 해당 Phase의 상세 스펙 참조
3. 각 항목 구현 전 관련 파일을 Read로 현재 상태 파악 (READ)
4. 가장 단순한 동작 구현 먼저 — 불필요한 추상화 금지 (IMPLEMENT)
5. 모든 항목 구현 후, 아래 검증 명령어를 실행 (GATE):
{chr(10).join('   ' + cmd for cmd in PHASE_VERIFY.get(phase_num, []))}
6. 검증 통과 시 TODO.md의 해당 항목을 [x]로 변경
7. 구현 결과가 PRD 스펙에서 벗어나지 않았는지 확인
8. git add -A && git commit

절대 규칙:
- 검증 없이 [x]로 체크 금지
- "통과했다"고 주장 금지 — 실제 명령어 출력 결과를 보여라
- 이미 동작하는 코드를 다시 짜지 마라 — 실패 시 되돌리지 않고 수정
- 질문하지 말고 판단하라
""".strip()

    # 프로젝트별 lint 규칙 가이드
    if LINT_RULES_GUIDE:
        base_prompt += f"""

Lint/타입 규칙 (반드시 숙지):
{LINT_RULES_GUIDE}
"""

    # Phase별 특별 규칙
    extra = PHASE_EXTRA_PROMPT.get(phase_num, "")
    if extra:
        base_prompt += f"""

Phase {phase_num} 특별 규칙:
{extra}
"""

    if retry_context:
        base_prompt += f"""

[RETRY] 이전 시도에서 검증 실패. 아래 에러를 분석하고 수정하라.

에러 메시지:
{retry_context[:3000]}

에러 해결 전략 (반드시 순서대로):
1. 에러 메시지를 정확히 읽고, 에러가 발생한 파일과 줄번호를 확인
2. 해당 파일을 Read로 열어서 실제 코드를 확인
3. 에러 원인이 코드 문제인지, 설정 문제인지(eslint/tsconfig/vite 등) 판단
4. 설정 문제라면 설정 파일을 수정
5. 코드 문제라면 해당 코드 수정
6. 수정 후 반드시 검증 명령어를 다시 실행하여 에러가 해결되었는지 확인
7. 같은 에러가 반복되면 접근 방식을 바꿔라 — 코드를 고쳤는데 안 되면 설정을 의심

주의:
- 같은 수정을 반복하지 마라 — 이전에 시도한 방법이 안 됐으면 다른 접근법을 써라
- "코드만 고치면 될 것 같다"는 가정을 버려라 — 설정, 의존성, 타입 정의 모두 의심
- 에러 1개를 확실히 잡고 다음으로 넘어가라 — 한꺼번에 추측성 수정 금지
"""

    base_prompt += f"""

Phase {phase_num}의 모든 항목이 완료되면 "PHASE_{phase_num}_DONE"을 출력하라.
"""
    return base_prompt

def run_claude(prompt: str, model: str, logger: logging.Logger,
               timeout: int = 1800, interactive: bool = False) -> tuple[int, str]:
    """Claude Code CLI를 실행하고 결과를 반환"""
    model_id = MODEL_MAP.get(model, model)

    if interactive:
        cmd = [
            CLAUDE_CMD,
            "--model", model_id,
            "--dangerously-skip-permissions",
        ]
    else:
        cmd = [
            CLAUDE_CMD,
            "--print",
            "--model", model_id,
            "--allowedTools", "Bash,Read,Write,Edit",
        ]

    logger.debug(f"Run: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(PROJECT_DIR),
            env={**os.environ, "CLAUDE_AUTO_ACCEPT": "1"}
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode, output
    except subprocess.TimeoutExpired:
        logger.warning(f"TIMEOUT ({timeout}s)")
        return -1, "TIMEOUT"
    except FileNotFoundError:
        logger.error(f"Claude Code CLI not found: {CLAUDE_CMD}")
        logger.error("Install: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)

def run_codex_assist(error_msg: str, phase_num: int, logger: logging.Logger) -> Optional[str]:
    """보조 에이전트(Codex)에게 도움 요청"""
    try:
        prompt = f"""
Phase {phase_num} 구현 중 다음 에러가 반복 발생합니다:

{error_msg[:2000]}

해결 방법을 구체적인 코드와 함께 알려주세요.
"""
        result = subprocess.run(
            [CODEX_CMD, "--print", prompt],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, cwd=str(PROJECT_DIR)
        )
        if result.returncode == 0:
            logger.info("Codex assist received")
            return result.stdout or ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.debug("Codex CLI not available — skip")
    return None

# ─── 에러 분석 ────────────────────────────────────────────

def detect_rate_limit(output: str) -> bool:
    patterns = ["rate limit", "429", "too many requests", "usage limit", "exceeded", "try again later"]
    output_lower = output.lower()
    return any(p in output_lower for p in patterns)

def extract_error_signature(output: str) -> str:
    """에러의 고유 시그니처를 추출 (같은 에러 반복 감지용)"""
    error_lines = [
        line.strip() for line in output.splitlines()
        if any(kw in line.lower() for kw in ["error", "failed", "cannot", "not found"])
    ]
    sig = "\n".join(error_lines[:10])
    return hashlib.md5(sig.encode()).hexdigest()[:12] if sig else ""

def extract_error_detail(output: str) -> str:
    """Claude에게 전달할 에러 상세 내용 추출 — 에러 종류별 집계 포함"""
    error_lines = []
    error_types: dict[str, int] = {}
    for line in output.splitlines():
        lower = line.lower()
        if any(kw in lower for kw in ["error", "failed", "cannot", "not found", "warning", "ts("]):
            error_lines.append(line.strip())
            # ESLint rule 이름 추출
            for part in line.split():
                if "/" in part and not part.startswith("/") and not part.startswith("D:"):
                    rule = part.strip("()")
                    error_types[rule] = error_types.get(rule, 0) + 1

    summary = ""
    if error_types:
        top_rules = sorted(error_types.items(), key=lambda x: -x[1])[:10]
        summary = "에러 종류별 집계 (가장 많은 순):\n"
        for rule, count in top_rules:
            summary += f"  {rule}: {count}건\n"
        summary += "\n가장 많은 에러 유형부터 먼저 해결하라.\n\n"

    return summary + "\n".join(error_lines[:40])

# ─── 메인 루프 ────────────────────────────────────────────

def run_phase(phase_num: int, model: str, logger: logging.Logger,
              skip_verify: bool = False, interactive: bool = False) -> bool:
    """단일 Phase를 실행하고 성공 여부를 반환"""
    logger.info(f"\n{'='*60}")
    logger.info(f"Phase {phase_num} START (model: {model})")
    logger.info(f"{'='*60}")

    timeout = PHASE_TIMEOUT.get(phase_num, 1800)
    error_history: list[str] = []
    last_error_detail = ""
    previous_attempts_summary: list[str] = []

    for attempt in range(1, MAX_RETRIES_PER_PHASE + 1):
        logger.info(f"\n  --- Attempt {attempt}/{MAX_RETRIES_PER_PHASE} ---")

        # 재시도 시 에러 컨텍스트 + 이전 시도 요약 포함
        retry_ctx = ""
        if attempt > 1 and last_error_detail:
            retry_ctx = last_error_detail
            if previous_attempts_summary:
                retry_ctx += "\n\n이전 시도에서 수정했지만 해결 안 된 파일:\n"
                retry_ctx += "\n".join(previous_attempts_summary[-3:])  # 최근 3회
        prompt = build_prompt(phase_num, retry_context=retry_ctx)

        returncode, output = run_claude(prompt, model, logger, timeout, interactive)

        # 출력 로그 저장
        output_file = LOG_DIR / f"phase{phase_num}_attempt{attempt}_{datetime.now().strftime('%H%M%S')}.log"
        output_file.write_text(output, encoding="utf-8")
        logger.debug(f"  Output saved: {output_file}")

        # Rate limit
        if detect_rate_limit(output):
            logger.warning(f"  Rate limit — waiting {RATE_LIMIT_WAIT_SECONDS}s")
            time.sleep(RATE_LIMIT_WAIT_SECONDS)
            continue

        # 완료 신호
        if f"PHASE_{phase_num}_DONE" in output:
            logger.info(f"  Completion signal detected")

            if skip_verify:
                logger.info("  Verification skipped (--skip-verify)")
                git_auto_commit(phase_num, logger)
                return True

            verified, verify_output = run_verification(phase_num, logger)
            if verified:
                git_auto_commit(phase_num, logger)
                logger.info(f"  Phase {phase_num} DONE (attempt {attempt})")
                return True
            else:
                logger.warning("  Completion signal found but verification failed — retry")
                last_error_detail = extract_error_detail(verify_output)
                # git diff로 이번 시도에서 변경된 파일 수집 (같은 실수 반복 방지)
                diff_result = subprocess.run(
                    ["git", "diff", "--name-only"], cwd=str(PROJECT_DIR),
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10
                )
                changed = (diff_result.stdout or "").strip()
                if changed:
                    previous_attempts_summary.append(
                        f"Attempt {attempt}: modified [{changed.replace(chr(10), ', ')}] → verification failed"
                    )
                continue

        # 에러 분석
        error_sig = extract_error_signature(output)
        error_history.append(error_sig)
        last_error_detail = extract_error_detail(output)

        # 같은 에러 반복 → 보조 에이전트
        recent = error_history[-STUCK_THRESHOLD:]
        if len(recent) >= STUCK_THRESHOLD and len(set(recent)) == 1 and recent[0]:
            logger.warning(f"  Same error {STUCK_THRESHOLD}x — calling assist agent")
            codex_help = run_codex_assist(last_error_detail, phase_num, logger)
            if codex_help:
                retry_ctx = f"""
이전 에러:
{last_error_detail}

보조 에이전트 제안:
{codex_help[:3000]}

이 제안을 참고하되, 맹목적으로 따르지 말고 판단하여 적용하라.
"""
                prompt = build_prompt(phase_num, retry_context=retry_ctx)
                returncode, output = run_claude(prompt, model, logger, timeout, interactive)
                if f"PHASE_{phase_num}_DONE" in output:
                    if skip_verify:
                        git_auto_commit(phase_num, logger)
                        return True
                    verified, _ = run_verification(phase_num, logger)
                    if verified:
                        git_auto_commit(phase_num, logger)
                        logger.info(f"  Phase {phase_num} DONE (with assist)")
                        return True

        logger.info(f"  Waiting {RETRY_DELAY_SECONDS}s before retry...")
        time.sleep(RETRY_DELAY_SECONDS)

    logger.error(f"  Phase {phase_num} FAILED — max retries reached")

    fail_file = LOG_DIR / "failed-phases.txt"
    with open(fail_file, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat()} | Phase {phase_num} | retries={MAX_RETRIES_PER_PHASE} | last_error={last_error_detail[:200]}\n")

    return False

# ─── 사전 점검 ────────────────────────────────────────────

def preflight_check(logger: logging.Logger) -> bool:
    """실행 전 필수 파일/도구 점검"""
    checks = []

    for f in [TODO_FILE, CLAUDE_MD, PRD_MD]:
        checks.append((f.name, f.exists()))

    try:
        result = subprocess.run([CLAUDE_CMD, "--version"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=10)
        checks.append(("Claude Code CLI", result.returncode == 0))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        checks.append(("Claude Code CLI", False))

    try:
        result = subprocess.run(["git", "status"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=10, cwd=str(PROJECT_DIR))
        checks.append(("Git repo", result.returncode == 0))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        checks.append(("Git repo", False))

    try:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=10)
        checks.append(("Node.js", result.returncode == 0))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        checks.append(("Node.js", False))

    logger.info("\nPreflight check:")
    all_ok = True
    for name, ok in checks:
        status = "OK" if ok else "FAIL"
        logger.info(f"  [{status}] {name}")
        if not ok:
            all_ok = False

    if not all_ok:
        logger.error("\nPreflight failed! Check items above.")

    return all_ok

# ─── Main ─────────────────────────────────────────────────

def main():
    global MAX_RETRIES_PER_PHASE
    parser = argparse.ArgumentParser(
        description="Ralph Loop Orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python harness.py                          Run all phases
  python harness.py --phase 4                Start from Phase 4
  python harness.py --phase 4 --end-phase 6  Run Phase 4~6 only
  python harness.py --model opus             Use Opus for all
  python harness.py --dry-run                Plan only, no execution
  python harness.py --interactive            Interactive mode
        """
    )
    parser.add_argument("--phase", type=int, help="Start phase number")
    parser.add_argument("--end-phase", type=int, help="End phase number")
    parser.add_argument("--model", choices=["sonnet", "opus"], help="Model for all phases")
    parser.add_argument("--dry-run", action="store_true", help="Plan only")
    parser.add_argument("--skip-verify", action="store_true", help="Skip verification (debug)")
    parser.add_argument("--interactive", action="store_true", help="Interactive mode")
    parser.add_argument("--max-retries", type=int, default=MAX_RETRIES_PER_PHASE, help="Max retries per phase")
    args = parser.parse_args()

    MAX_RETRIES_PER_PHASE = args.max_retries

    logger = setup_logging()

    logger.info("")
    logger.info("  Ralph Loop Orchestrator")
    logger.info(f"  Project: {PROJECT_DIR}")
    logger.info(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("")

    if not args.dry_run:
        if not preflight_check(logger):
            sys.exit(1)

    phases = parse_todo()
    print_progress(phases, logger)

    if args.phase:
        start_phase = args.phase
    else:
        start_phase = get_next_phase(phases)
        if start_phase is None:
            logger.info("All phases already complete!")
            return

    end_phase = args.end_phase or max(phases.keys())

    logger.info(f"\nPlan: Phase {start_phase} -> Phase {end_phase}")
    logger.info(f"  Mode: {'interactive' if args.interactive else '--print'}")
    logger.info(f"  Retries: max {MAX_RETRIES_PER_PHASE}/phase")

    if args.dry_run:
        logger.info("\n--- Dry Run ---")
        total_time = 0
        for p in range(start_phase, end_phase + 1):
            if p in phases and phases[p]["done"] < phases[p]["total"]:
                model = args.model or PHASE_MODEL.get(p, "sonnet")
                timeout = PHASE_TIMEOUT.get(p, 1800)
                total_time += timeout
                remaining = phases[p]["total"] - phases[p]["done"]
                logger.info(f"  Phase {p:2d}: {model:6s} | {remaining} items | timeout {timeout//60}min")
        logger.info(f"\n  Max estimated: {total_time//3600}h {(total_time%3600)//60}min")
        return

    # Phase 루프
    start_time = datetime.now()
    failed_phases = []

    for phase_num in range(start_phase, end_phase + 1):
        if phase_num not in phases:
            continue
        if phases[phase_num]["done"] == phases[phase_num]["total"]:
            logger.info(f"Phase {phase_num} already done — skip")
            continue

        model = args.model or PHASE_MODEL.get(phase_num, "sonnet")
        success = run_phase(
            phase_num, model, logger,
            skip_verify=args.skip_verify,
            interactive=args.interactive
        )

        if success:
            phases = parse_todo()
            print_progress(phases, logger)
        else:
            failed_phases.append(phase_num)
            # 초기 Phase 실패 시 중단 (기반이 없으면 진행 무의미)
            if phase_num <= 3:
                logger.error(f"Foundation Phase {phase_num} failed — stopping")
                logger.info(f"  Resume: python harness.py --phase {phase_num}")
                break
            else:
                logger.warning(f"Phase {phase_num} failed — continuing to next")

    elapsed = datetime.now() - start_time
    logger.info(f"\n{'='*60}")
    logger.info("Final Result")
    logger.info(f"{'='*60}")

    if not failed_phases:
        logger.info("All phases succeeded!")
    else:
        logger.warning(f"Failed phases: {failed_phases}")
        logger.info(f"  Resume: python harness.py --phase {failed_phases[0]}")

    logger.info(f"Elapsed: {str(elapsed).split('.')[0]}")
    logger.info(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if __name__ == "__main__":
    main()
