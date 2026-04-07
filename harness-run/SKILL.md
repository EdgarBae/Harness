---
name: harness-run
description: 기존 harness.py의 상태를 점검하고 실행 준비를 한다. 로그 정리, TODO 리셋, 사전 검증 등. "harness 돌려", "다시 처음부터", "harness 상태 확인" 등의 요청 시 사용.
user-invocable: true
argument-hint: [action: check|reset|clean]
---

# Harness Run — 실행 준비 & 상태 관리

기존 harness.py 프로젝트의 실행 상태를 점검하고 관리한다.

## 액션별 동작

### `check` (기본) — 상태 점검
1. `harness.py` 존재 확인
2. `TODO.md` 파싱 → 현재 진행률 출력
3. `CLAUDE.md`, `PRD.md` 존재 확인
4. `logs/` 최근 로그 확인 (마지막 실행 시간, 실패 Phase)
5. Claude Code CLI 설치 확인 (`claude --version` or `claude.cmd --version`)
6. Node.js/Python/Git 버전 확인
7. `node_modules/` 존재 확인 (npm install 필요 여부)
8. **자가 치유 설정 점검**:
   - `LINT_RULES_GUIDE`가 비어있으면 경고 (에이전트가 lint 규칙 모를 수 있음)
   - `PHASE_EXTRA_PROMPT`에 현재 Phase 항목이 있는지 확인
   - ESLint auto-fix 대상 검증 명령어에 `eslint`이 포함되어 있는지 확인
9. 결과를 테이블로 보고

### `reset` — 처음부터 다시
1. `TODO.md`의 모든 `[x]`를 `[ ]`로 리셋
2. `logs/` 내 파일 전체 삭제
3. `logs/failed-phases.txt` 삭제
4. 리셋 완료 보고

### `clean` — 로그만 정리
1. `logs/` 내 파일 전체 삭제
2. 정리 완료 보고

## 실행 가이드 출력

점검 완료 후 실행 명령어 안내:
```bash
# 전체 실행
python harness.py

# 특정 Phase부터
python harness.py --phase {다음_미완료_Phase}

# 건조 실행 (계획만)
python harness.py --dry-run

# Opus로 실행 (복잡한 Phase)
python harness.py --phase {N} --model opus

# 대화형 모드 (디버깅)
python harness.py --phase {N} --interactive
```

## 실패 복구 가이드

`logs/failed-phases.txt`가 있으면:
1. 마지막 실패 Phase와 에러 내용 읽기
2. 해당 Phase의 최근 로그 파일 분석
3. **에러가 코드 문제인지 설정(lint/tsconfig) 문제인지 판별** — 반복 실패의 근본 원인은 설정일 수 있음
4. 에러 원인 요약 + 수정 제안
5. 설정 문제라면:
   - ESLint 규칙 완화가 필요한지 검토 (예: `max-lines-per-function` 값 조정)
   - `LINT_RULES_GUIDE`에 위반 규칙 명시하여 에이전트에게 알려줄 것을 제안
   - `PHASE_EXTRA_PROMPT`에 해당 Phase 특별 지시 추가 제안
6. `--phase {실패Phase}` 명령어 안내

## TODO.md 진행률 시각화

```
Phase  1: [████████████] 12/12
Phase  2: [██░░░░░░░░░░]  2/10
Phase  3: [░░░░░░░░░░░░]  0/8
...
전체: 14/80 (17.5%)
```
