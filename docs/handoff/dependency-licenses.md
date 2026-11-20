# 직접 의존성 라이선스·고지 후보

2026-11-10 현재 선언과 로컬 격리 설치본을 기준으로 수집한 자료다. [기계 판독 목록](third-party-notices/inventory.json)에는 패키지명·용도·선언·잠금·설치 버전, metadata의 라이선스 선언, 원본 상대경로, 사본 경로, 크기와 SHA-256을 적었다. 각 원문은 [third-party-notices](third-party-notices/)에 배포 패키지와 버전별로 원형 보존했다. 법적 의무 판단이나 준수 완료를 뜻하지 않는다.

- API 직접 배포 패키지 19개: 실행 15개, 개발 4개. 선언은 `apps/api/pyproject.toml`, 잠금은 `apps/api/uv.lock`이다. SQLAlchemy의 `asyncio` extra를 포함한 선언을 대조했다. 배포 패키지명은 Python import명과 다를 수 있다. 19개 모두 해당 잠금·설치 버전이 일치하고 원문 후보가 있다.
- 웹 직접 npm 패키지 23개: 실행 6개, 개발 17개. 선언은 `apps/web/package.json`, 잠금은 `apps/web/pnpm-lock.yaml`의 두 번째 YAML 문서에 있는 `.` importer다. 첫 문서는 pnpm 자체를 기록한다. 23개 모두 선언·잠금·설치 버전이 일치하고 원문 후보가 있다. scoped 이름도 그대로 기록한다. 설치 결과의 `node_modules/.pnpm/lock.yaml`은 보조 증거다.
- 설치본 전체 직접 의존성 42개에서 파일 48개를 수집했다. Python의 `RECORD`에 나타난 LICENSE·COPYING·NOTICE·AUTHORS 계열과 npm 설치 패키지의 같은 이름 계열을 확인했다. 설치된 Python metadata의 `License-File`도 목록에 기록했다. 미설치 직접 의존성은 없으며 별도 marker에 따른 제외는 없었다. extra가 끌어오는 전이 패키지는 이 수에 넣지 않는다.
- OpenCV wheel의 `LICENSE-3RD-PARTY.txt` 두 사본과 Pillow의 여러 포함 라이브러리 고지가 들어 있는 LICENSE는 별도 분류했다. wheel별 내장 고지의 발견 사례이지 모든 native wheel 또는 전이 의존성 전체 조사 결과는 아니다.

원본 기준 경로는 격리 설치본의 `source/.venv/lib/python3.12/site-packages` 및 `source/apps/web/node_modules`다. npm 경로는 pnpm 저장 디렉터리 내부로 해석한 상대경로다. 이 격리 설치본은 최종 배포물 자체가 아니며, 환경별 wheel·선택 의존성·실제 포함 파일은 달라질 수 있다. 새 다운로드 없이 현재 자료만 사용했다.

42개 각각에서 LICENSE 또는 COPYING 이름의 본문 후보를 확보했다. metadata나 AUTHORS만 확보된 항목과 본문 후보가 없는 항목은 각각 0개다. SQLAlchemy의 `asyncio` extra가 추가하는 greenlet 등 보조 배포 패키지는 직접 선언 패키지 수에 포함하지 않는다. 아래 별도 API 런타임 메타데이터 추적 결과에서 확인했다. native wheel 내장 구성요소 전체의 고지 범위는 미확인이다. 다만 파일이 존재한다는 사실만으로 원문·고지의 완전성, 라이선스 해석 또는 배포 조건 충족을 보장하지 않는다. 전이 의존성 전수 검토, 최종 소스·빌드·wheel의 실제 포함 범위에 따른 고지·조건 판단, 프로젝트 자체 라이선스 결정은 미완료다. 수신자에게는 이 문서와 목록, 연결된 원문을 함께 제공하고 최종 배포 형태를 확정한 뒤 별도 검토해야 한다.

## API 런타임 메타데이터 추적

[별도 런타임 목록](third-party-notices/runtime-transitive-inventory.json)은 API 런타임 선언 15개에서 출발해 격리 설치본의 `Requires-Dist`를 Darwin arm64, CPython 3.12.8 조건으로 평가한 결과다. 선택된 패키지의 요청 extra를 합산하고 새 extra가 생기면 다시 평가했다. 자식에게는 해당 요구사항에 명시된 extra만 전달했다. 개발용 직접 선언은 출발점에서 제외했다.

선택된 배포 패키지는 직접 15개를 포함해 44개다. 메타데이터 요구사항 296개 중 47개 edge가 선택됐고 선택·제외 및 parent extra별 marker 결과는 목록에 있다. 각 선택 패키지의 설치·잠금 버전과 선택 요구사항의 버전 조건이 일치했다. 추가 전이 패키지 29개에서 원문·고지 72개를 수집했고 직접 패키지의 기존 원문 21개를 재사용했다. greenlet과 redis가 요청한 hiredis가 포함된다. 목록상의 미확인·누락은 0건이다.

이 결과는 해당 설치본의 metadata상 폐쇄 범위다. Linux/Vercel, npm 전이 의존성, metadata에 드러나지 않는 native 내장 구성요소 전수, 실제 최종 배포물과 법적 준수는 확인하지 않았다.

## npm 설치 그래프 추적

[별도 npm 목록](third-party-notices/npm-transitive-inventory.json)은 검토 후보의 `apps/web`에서 실행·개발 직접 선언 23개와 설치된 의존성·선택 의존성·peer 링크를 추적한 결과다. Darwin arm64 설치본에서 도달한 pnpm 인스턴스 265개와 저장 디렉터리를 독립 열거한 265개가 일치했다. 이 환경에서는 고유 패키지명·버전도 265개다. 잠금 파일의 패키지 289개 중 나머지 24개는 현재 OS 또는 CPU와 맞지 않는 선택 패키지다. 설치 metadata와 잠금에서 모두 선택 peer로 확인한 미설치 peer는 27개이며 필수 의존성 링크 누락은 발견하지 않았다. 잠금 integrity는 기록된 값이며 다운로드 원본 바이트의 검증을 뜻하지 않는다.

265개 중 256개에서 LICENSE 또는 COPYING 이름의 본문 후보를 찾았다. 별도로 `imurmurhash@0.1.4`와 `esrecurse@4.3.0`은 README에 라이선스 전문이 있어 README 전체를 보존했다. 나머지 `natural-compare@1.4.0`, `saxes@6.0.0`, `change-case@5.4.4`, `@humanfs/types@0.15.0`, `@rolldown/binding-darwin-arm64@1.2.15`, `uri-js-replace@1.0.1`, `@redocly/openapi-core@1.34.21`의 전문은 설치본에서 확인하지 못했다. 이름별 본문·고지 후보 259개와 README 2개를 기록했으며, 기존 직접 npm 고지 23개는 패키지·버전·크기·해시를 확인해 재사용하고 나머지 238개를 원형 복사했다. 제출 검토에서 OpenAPI 라이선스 URL 검사 테스트가 파일명으로 잘못 분류된 사실을 확인해 목록과 사본에서 제외했다. `type-fest@4.41.0`의 선언은 `(MIT OR CC0-1.0)`이다. 본문 후보의 존재는 해당 라이선스의 해석이나 모든 고지 의무 충족을 뜻하지 않는다.

목록은 설치 링크의 출발점별 도달 범위를 기록한다. 실행 직접 선언에서 도달한 17개에는 설치된 선택·peer 링크가 포함되며 실제 배포 번들 포함 여부는 아니다. Linux/Vercel의 설치 결과, 최종 소스·번들 범위와 native 패키지 내장 구성요소의 고지는 별도 확인이 필요하다. 위 API 런타임 절의 npm 미확인 문장은 그 조사를 수행한 당시 범위를 가리킨다.

### 정확한 버전의 외부 원문 보완

2026-11-10 UTC에 공식 npm 버전 metadata가 가리키는 커밋과 해당 커밋의 패키지 식별을 대조해 `saxes@6.0.0`과 `change-case@5.4.4`의 원문 2개를 추가했다. 후자는 같은 커밋의 workspace 선언과 monorepo README가 해당 패키지를 포함하고 MIT로 명시한다. 출처 URL·커밋·패키지 manifest 해시·원문 해시는 목록에 기록했다. 위 설치본 원문 261개와는 별도의 자료이며, 잠금 integrity는 registry metadata와 일치하지만 tarball 바이트나 서명을 검증한 것은 아니다.

`natural-compare@1.4.0`, `@humanfs/types@0.15.0`, `@rolldown/binding-darwin-arm64@1.2.15`, `uri-js-replace@1.0.1`의 버전에 적용되는 원문은 아직 확보하지 못했다. binding의 npm 출처 증명에는 커밋 주장이 있지만 서명 검증과 해당 바인딩에 대한 원문 적용 근거는 확인하지 못했다. native 내장 구성요소 범위도 여전히 미확인이다.

같은 날 `@rolldown/binding-darwin-arm64@1.2.15`의 registry가 가리키는 출처 증명에서 패키지 식별자와 SHA-512 주장이 registry·잠금 metadata와 일치했다. 증명이 주장한 커밋의 `rolldown` 1.2.15 manifest는 MIT, 바인딩 패키지명과 Darwin arm64 대상을 명시하고, 배포 workflow는 바인딩 생성·배포 및 라이선스 복사를 명시한다. 그 커밋의 LICENSE와 직접 연결된 THIRD-PARTY-LICENSE를 외부 원문으로 별도 추가했다. 출처 증명의 서명이나 실제 binary·tarball 바이트는 검증하지 않았으며, native 내장 구성요소의 고지가 모두 포함됐다는 뜻도 아니다. 바로 앞 문단의 binding 미확보 서술은 이 추가 확인 이전의 상태다. 외부 보완 뒤에는 `natural-compare@1.4.0`, `@humanfs/types@0.15.0`, `uri-js-replace@1.0.1`, `@redocly/openapi-core@1.34.21` 네 패키지의 원문이 미확보다.
