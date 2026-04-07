---
name: harness-init
description: 새 프로젝트에 자율 개발 harness 구조를 세팅한다. harness.py, TODO.md, PRD.md 템플릿을 생성하고 CLAUDE.md에 연동 설정을 추가한다. "harness 세팅", "자율 개발 구조 만들어" 등의 요청 시 사용.
user-invocable: true
argument-hint: [project-description]
---

# Harness Init — 자율 개발 오케스트레이터 세팅

새 프로젝트에 Ralph Loop 기반 자율 개발 harness 구조를 세팅한다.

## 세팅할 파일 (4개)

### 1. `harness.py` — 자율 오케스트레이터
Claude Code CLI를 Phase별로 호출하여 자율 개발하는 Python 스크립트.

핵심 구조:
```
설정 → 사전점검 → TODO 파싱 → Phase 루프 {
  프롬프트 생성 → Claude 실행(--print) → 완료 감지 → 검증 → git commit
  실패 시: 에러 추출 → 재시도 (최대 N회) → 보조 에이전트 호출
}
```

**필수 포함 기능:**
- `CLAUDE_CMD`: Windows는 `claude.cmd`, Unix는 `claude` 자동 감지
- `subprocess.run`: 모든 호출에 `encoding="utf-8", errors="replace"` 필수 (Windows cp949 방지)
- `(result.stdout or "") + (result.stderr or "")` 패턴 (NoneType 방지)
- `--print` 모드에서 프롬프트는 `input=prompt` (stdin)으로 전달
- Phase별 타임아웃, 모델(sonnet/opus) 매핑, 검증 명령어 매핑
- Rate limit 감지 + 대기
- 같은 에러 반복 감지 → 보조 에이전트(Codex) 자동 호출
- 로그 파일 자동 생성 (`logs/` 디렉토리)
- `--phase N`, `--end-phase M`, `--dry-run`, `--model`, `--skip-verify`, `--interactive` CLI 옵션

**harness.py 템플릿의 커스텀 포인트** (프로젝트별 수정):
```python
# 1. Phase별 타임아웃 (초)
PHASE_TIMEOUT = {
    1: 1200,   # 20분
    2: 1800,   # 30분
    # ...
}

# 2. Phase별 모델
PHASE_MODEL = {
    1: "sonnet",
    # 마지막 통합 Phase는 opus 추천
    9: "opus",
}

# 3. Phase별 검증 명령어
PHASE_VERIFY = {
    1: ["npx tsc --noEmit", "npx vite build"],
    2: ["npx eslint src/"],
    # Python 프로젝트면:
    # 1: ["poetry run ruff check .", "poetry run mypy ."],
}

# 4. Git 커밋 메시지
commit_messages = {
    1: "feat(init): project scaffolding",
    # ...
}

# 5. Lint/타입 규칙 가이드 (프롬프트에 포함 — --print 에이전트가 규칙 숙지용)
LINT_RULES_GUIDE = """
- `||` 대신 `??` 사용 (boolean 예외)
- console.log 금지 → logger 사용
""".strip()

# 6. Phase별 특별 규칙 (해당 Phase에만 추가되는 프롬프트)
PHASE_EXTRA_PROMPT = {
    2: "테스트 코드만 작성. 구현은 하지 않는다.",
}
```

**자가 치유(Self-Healing) 기능** (템플릿에 내장):
- **ESLint auto-fix**: 검증 전 `npx eslint src/ --fix` 자동 실행 → trivial 에러(세미콜론, 콤마 등) 사전 제거
- **에러 종류별 집계**: 재시도 프롬프트에 ESLint rule별 에러 건수 Top 10 포함 → 에이전트가 우선순위 판단 가능
- **이전 시도 요약**: git diff로 변경 파일 추적 → 재시도 시 "수정했지만 해결 안 된 파일" 정보 제공 → 같은 실수 반복 방지
- **Lint 규칙 가이드 슬롯**: `LINT_RULES_GUIDE`에 프로젝트별 규칙 명시 → --print 에이전트가 매 시도마다 규칙 숙지
- **Phase별 특별 규칙 슬롯**: `PHASE_EXTRA_PROMPT`로 특정 Phase에만 적용되는 지시 추가 가능

### 2. `TODO.md` — Phase별 체크리스트
```markdown
# TODO.md — {프로젝트명}

> Ralph Loop 실행 시 이 파일의 `[ ]` 항목을 순서대로 구현한다.
> 각 Phase 완료 후 검증 명령어를 실행하고, 통과 시에만 `[x]`로 변경한다.

---

## Phase 1: {Phase 이름}

- [ ] 항목 1
- [ ] 항목 2

**검증**:
```bash
{검증 명령어}
```

- [ ] git commit: "{커밋 메시지}"

---

## 완료 조건
아래 **모든 조건**이 충족될 때만 `{PROJECT}_COMPLETE` 출력:
1. 위 TODO 항목이 모두 `[x]`로 체크됨
2. 모든 검증 명령어 에러 0
```

**규칙:**
- Phase당 항목 3~20개 (너무 적으면 합치고, 너무 많으면 분할)
- 각 Phase 마지막에 `git commit` 항목 포함
- 완료 신호: `PHASE_{N}_DONE` (harness.py가 감지)

### 3. `PRD.md` — 상세 스펙 문서
TODO.md는 체크리스트, PRD.md는 각 항목의 상세 구현 스펙.
Claude Code가 "무엇을 어떻게" 구현해야 하는지 알 수 있도록 작성.

### 4. `CLAUDE.md` 업데이트
기존 CLAUDE.md에 harness 관련 섹션 추가:
```markdown
## 참조 문서 (반드시 읽고 시작)
- `PRD.md` — 프로젝트 상세 스펙
- `TODO.md` — Phase별 구현 체크리스트

## 검증 명령어 (매 Phase 완료 후 반드시 실행)
{프로젝트에 맞는 검증 명령어}
```

## 실행 절차

1. 사용자에게 프로젝트 설명을 확인: $ARGUMENTS
2. Phase 구성을 제안하고 사용자 확인
3. 위 4개 파일을 생성
4. `logs/` 디렉토리 생성
5. `.gitignore`에 `logs/` 추가
6. 사용법 안내:
   ```bash
   python harness.py              # 전체 실행
   python harness.py --dry-run    # 계획만 출력
   python harness.py --phase 3    # Phase 3부터
   python harness.py --model opus # Opus로 실행
   ```

## Windows 필수 주의사항

harness.py 작성 시 아래 사항을 반드시 반영:
1. `CLAUDE_CMD = "claude.cmd" if os.name == "nt" else "claude"`
2. 모든 `subprocess.run`에 `encoding="utf-8", errors="replace"` 추가
3. `result.stdout`이 None일 수 있으므로 `(result.stdout or "")` 패턴 사용
4. `--print` 모드에서 프롬프트는 positional arg가 아닌 `input=prompt`(stdin)으로 전달
5. `shell=True` 사용 시 Windows 경로의 백슬래시 주의
