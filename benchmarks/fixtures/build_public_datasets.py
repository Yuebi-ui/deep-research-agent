"""Build transparent, fictional/public benchmark tasks and retrieval fixtures.

All scenario entities and .example sources are invented. This is not an E2E trace
or evidence of current-world facts. Committed data are deterministic and editable.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'datasets'

# Invented organizations, non-factual numbers, deliberately confusable old/new facts.
# name, alias, sector, metric, old, new, unit, method, partner, activity, location
SCENARIOS = [
 ('Aster Delta Works','ADW','grid storage','storage capacity',72,96,'MWh','iron-air cycling','Maple Ridge Grid','grid balancing','North Quay'),
 ('Saffron Circuit Lab','SCL','semiconductor packaging','interposer yield',81,91,'percent','glass-substrate bonding','Cedar Wafer Studio','pilot packaging','East Basin'),
 ('Juniper Tide Systems','JTS','water treatment','reuse volume',38,54,'ML per day','membrane bioreactors','Harbor Water Trust','wastewater reuse','Delta Harbor'),
 ('Orchid Harbor Robotics','OHR','warehouse robotics','pick rate',420,575,'units per hour','vision-guided gripping','Birch Parcel Network','parcel sorting','Bayside'),
 ('Lumen Orchard Cloud','LOC','cloud infrastructure','reserved compute capacity',1400,1850,'GPU hours per day','workload-aware placement','Raven Data Co-op','scientific computing','Lakeside'),
 ('Copper Fern Logistics','CFL','cold chain','monitored shipments',830,1120,'lots per month','sensor fusion tracking','Spruce Grocer Alliance','vaccine transport','West Field'),
 ('Mistral Reef Energy','MRE','offshore wind','tested output',18,27,'MW','floating mooring control','Pine Coast Energy','offshore pilot','Outer Bay'),
 ('Amber Vale Health','AVH','telemedicine','served clinics',24,39,'clinics','store-and-forward triage','Silver Meadow Trust','remote consultation','Hill County'),
 ('Quartz Meadow Finance','QMF','fintech risk','reviewed applications',560,720,'cases per day','explainable anomaly detection','Willow Credit Union','credit assessment','Central Ward'),
 ('Violet Compass Rail','VCR','urban transit','daily passenger capacity',6300,8100,'riders','adaptive platform scheduling','Northbrook Transit','metro interchange','Old Junction'),
 ('Nimbus Clover Farms','NCF','precision agriculture','irrigated acreage',480,660,'hectares','soil-moisture targeting','Elm Farmers Co-op','precision irrigation','Prairie Edge'),
 ('Tamarind Beacon Aero','TBA','earth observation','processed scenes',900,1240,'images per day','cloud-mask inference','Meridian Survey Guild','flood mapping','Summit Reach'),
 ('Boreal Echo Security','BES','cybersecurity','covered endpoints',12000,16500,'devices','behavioral detection','Kestrel Audit Group','endpoint defense','Northern Campus'),
 ('Indigo Willow Learning','IWL','education technology','active classrooms',180,260,'rooms','retrieval-guided tutoring','Canyon Teaching Network','classroom pilots','River District'),
 ('Silver Fir Maritime','SFM','shipping','tracked containers',3400,4600,'TEU','AIS-event reconciliation','Harborway Shipping','port visibility','South Terminal'),
 ('Coral Ember Materials','CEM','low-carbon cement','trial output',14,21,'kilotonnes','calcined-clay blending','Moss Building Partners','construction materials','Pillar Yard'),
 ('Blue Hazel Audio','BHA','speech systems','processed audio',540,790,'hours per day','streaming diarization','Lark Accessibility Studio','meeting transcription','Civic Studio'),
 ('Kite River Compute','KRC','edge inference','active gateways',320,450,'nodes','int8 quantization','Bamboo Wireless','factory sensing','Industrial Ring'),
 ('Mallow Sunrise Mobility','MSM','charging networks','operating stations',75,108,'stations','smart charge scheduling','Oak City Utilities','fleet charging','South Loop'),
 ('Poppy Sand Insurance','PSI','insurance operations','reviewed claims',110,165,'cases per day','document classification','Clover Claims Office','claims triage','Metro East'),
 ('Sable Coast Research','SCR','coastal monitoring','sampled shoreline',52,74,'kilometers','multispectral shoreline analysis','Tidal Ecology Group','erosion mapping','Seabird Point'),
 ('Apricot Peak Metals','APM','green steel','prototype production',7,11,'tonnes per day','hydrogen direct reduction','Bracken Metals Supply','low-carbon steel pilot','Foundry Bay'),
 ('Bamboo Valley Identity','BVI','digital identity','issued credentials',18000,25500,'credentials','selective disclosure','Harbor Identity Commons','credential issuance','Cloud Quarter'),
 ('Dawn Pepper Retail','DPR','retail analytics','daily orders',2600,3400,'orders','demand forecasting','Evergreen Merchant Group','inventory planning','Market Borough'),
 ('Cobalt Ridge Satellite','CRS','satellite communications','relay uptime',97,99,'percent','adaptive beam steering','Polar Antenna Collective','remote connectivity','Frost Plateau'),
 ('Golden Maple Archive','GMA','digital archives','indexed documents',85000,121000,'records','hybrid metadata indexing','Cedar Museum Union','catalog digitization','Old Town'),
 ('Teal Blossom Bio','TBB','bioprocessing','fermentation throughput',430,610,'liters per batch','inline spectroscopy','Granite Biofoundry','enzyme manufacturing','Lab North'),
 ('Dusk Olive Port','DOP','port management','daily berth calls',26,34,'vessels','berth optimization','Seaway Port Co-op','terminal scheduling','Inner Harbor'),
 ('Rose Clay Housing','RCH','building retrofit','audited buildings',120,176,'sites','thermal envelope simulation','Elm Community Housing','energy renovation','West Ridge'),
 ('Crimson Vale Open','CVO','developer tooling','weekly CI jobs',14000,19500,'runs','incremental dependency caching','Pine Developer Commons','open-source CI','Tech Quarter'),
]

TASK_GROUPS = {
 'agent_systems': [
  'Compare graph-based orchestration and role-based multi-agent delegation for long-running research tasks under strict audit requirements.',
  'Assess how agent tool-call retries interact with non-idempotent side effects and propose safe recovery boundaries.',
  'Evaluate checkpoint strategies for interruptible human-in-the-loop agents with concurrent worker restarts.',
  'Contrast evidence-pack writing against unrestricted long-context writing for citation-heavy research reports.',
  'Investigate agent prompt-injection threat models when retrieved historical memory contains hostile instructions.',
  'Analyze when a planner-executor-verifier workflow outperforms a single tool-using agent for ambiguous tasks.',
 ],
 'retrieval_memory': [
  'Compare BM25, multilingual embeddings, and hybrid retrieval on entity-rich technical questions with Chinese abbreviations.',
  'Design an evaluation for temporal knowledge retrieval when the same metric changes across source revisions.',
  'Review evidence-provenance models suitable for claim-to-document traceability in a research assistant.',
  'Assess the quality and operational costs of report-level versus section-level document indexing.',
  'Evaluate episodic memory for reducing repeated searches without propagating unsupported past conclusions.',
  'Analyze long-term memory deletion and tenant separation requirements for a shared research service.',
 ],
 'cloud_systems': [
  'Compare Redis Streams and database-backed queues for at-least-once AI workload processing.',
  'Evaluate lease fencing versus heartbeat-only recovery for agent workers using distributed checkpoints.',
  'Assess infrastructure bottlenecks in high-concurrency streaming LLM workloads under constrained GPU memory.',
  'Compare asynchronous memory consolidation to synchronous storage on user-visible latency and reliability.',
  'Explain deployment options for local inference plus cloud fallback while protecting API credentials.',
  'Design an observability plan for agent node latency, tool failure, cost attribution, and data retention.',
 ],
 'security': [
  'Assess security implications of giving a web-searching agent shell or file access in untrusted environments.',
  'Compare secret-management approaches for containerized AI applications with public code repositories.',
  'Evaluate adversarial citation insertion attacks against systems that validate only source URL presence.',
  'Review supply-chain risks in AI Python dependency resolution and reproducible model deployment.',
  'Develop a red-team test matrix for cross-tenant retrieval leakage and memory poisoning in research assistants.',
  'Assess trade-offs between strict structured outputs and permissive parser recovery for external model responses.',
 ],
 'industry_energy': [
  'Compare battery storage technologies for long-duration grid balancing using cited deployment constraints.',
  'Review limitations of hydrogen direct reduction as a pathway to low-carbon primary steel.',
  'Analyze offshore floating wind operations versus fixed-bottom wind under different sea-depth assumptions.',
  'Compare industrial wastewater reuse strategies across membrane, biological, and thermal treatment methods.',
  'Assess supply-chain vulnerabilities for power semiconductor packaging under capacity fluctuations.',
  'Compare demand-side flexibility policies for high-renewables power systems using documented case studies.',
 ],
 'health_science': [
  'Evaluate privacy and clinical oversight requirements for remote patient triage assistants.',
  'Compare observational and randomized evidence for public-health interventions with explicit uncertainty.',
  'Assess how systematic review updates should supersede older biomedical evidence in a knowledge repository.',
  'Analyze barriers to deploying AI-enabled diagnostics in low-connectivity healthcare settings.',
  'Review reproducibility practices for machine-learning studies in genomics and biomedical imaging.',
  'Evaluate safeguards for automated summaries of medical literature when evidence strength differs.',
 ],
 'economics_policy': [
  'Compare central-bank communications and published economic indicators when assessing policy changes over time.',
  'Analyze methodological issues in comparing energy subsidies across jurisdictions and fiscal years.',
  'Evaluate the impact of source publication lag on real-time labor market research.',
  'Compare global digital-identity governance approaches with emphasis on user consent and revocation.',
  'Assess which claims about AI regulation depend on legal jurisdiction and effective dates.',
  'Review uncertainty communication in forecasts that combine multiple macroeconomic models.',
 ],
 'infrastructure_transport': [
  'Compare berth allocation methods for container ports during demand surges and weather disruptions.',
  'Analyze auditability requirements for automated rail platform scheduling and passenger crowd management.',
  'Review resilience indicators for cold-chain logistics serving geographically dispersed clinics.',
  'Evaluate satellite connectivity trade-offs for remote coastal monitoring stations.',
  'Compare urban electric-fleet charging schedules under time-of-use pricing constraints.',
  'Assess methods to validate public transit service quality using incomplete open data.',
 ],
 'education_society': [
  'Compare retrieval-guided tutoring and conventional digital practice systems for classroom support.',
  'Analyze the reproducibility of claims about AI-assisted grading across school levels.',
  'Evaluate how open educational resources can be indexed without confusing copyright with public access.',
  'Review the strengths and limitations of automated multilingual accessibility captions.',
  'Assess evidence for remote-learning interventions when reported outcomes use incompatible measures.',
  'Design source-quality criteria for public-facing summaries of disputed scientific topics.',
 ],
 'data_analysis': [
  'Compare experimental protocols for evaluating citation grounding at statement level and document level.',
  'Analyze how to avoid data leakage when tuning a retrieval system against a public benchmark.',
  'Evaluate performance reporting with confidence intervals for small AI agent evaluation sets.',
  'Compare paired and unpaired experiments for latency and search-call measurements under nondeterministic outputs.',
  'Design an ablation plan separating retrieval quality, downstream report quality, and runtime reliability.',
  'Assess the limitations of LLM-as-judge agreement as a replacement for independent human annotation.',
 ],
}

ASPECTS = {
 'agent_systems': ['explicit workflow roles and state transitions', 'failure cases and mitigations', 'evidence from primary technical sources'],
 'retrieval_memory': ['retrieval or data model trade-offs', 'evaluation design and metrics', 'limitations and failure modes'],
 'cloud_systems': ['architecture and operational assumptions', 'reliability or performance implications', 'deployment constraints'],
 'security': ['threat model and attacker capabilities', 'defenses and residual risks', 'independent primary references'],
 'industry_energy': ['technical alternatives and assumptions', 'field evidence and constraints', 'uncertainty and source dates'],
 'health_science': ['study quality and evidence limits', 'applicability to deployment settings', 'safe handling of uncertain claims'],
 'economics_policy': ['jurisdiction and time period', 'primary statistics or regulations', 'counterarguments and limitations'],
 'infrastructure_transport': ['operational context and alternatives', 'quantitative indicators where supported', 'reliability trade-offs'],
 'education_society': ['relevant populations and outcomes', 'quality of supporting research', 'known limitations'],
 'data_analysis': ['metric definitions and denominators', 'experimental controls', 'statistical uncertainty'],
}


def dump_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r, ensure_ascii=False, sort_keys=True) + '\n' for r in rows), encoding='utf-8')


def build():
    corpus = []
    queries = []
    for i, (name, alias, sector, metric, old, new, unit, method, partner, activity, location) in enumerate(SCENARIOS, 1):
        key = f's{i:03d}'
        date_old, date_new = '2024-06-30', '2025-06-30'
        base = dict(entity_id=key, entity_name=name, entity_aliases=[alias], sector=sector,
                    synthetic=True, source_kind='fictional_fixture')
        rows = [
            (f'{name}: historical {metric}', f'In {location}, {name} ({alias}) recorded {old} {unit} of {metric} at the {date_old} reporting date. The value is historical; a later release reports a different figure.', date_old, 'metric'),
            (f'{name}: updated {metric}', f'In {location}, {name} ({alias}) recorded {new} {unit} of {metric} at the {date_new} reporting date. This later figure supersedes the previous dated measurement for current-year comparisons, without erasing the earlier record.', date_new, 'metric'),
            (f'{name}: technical method', f'For {sector}, {name} ({alias}) used {method} in its {activity} pilot. The method was selected for inspectable operational controls rather than a claim of universal superiority.', date_new, 'method'),
            (f'{name}: consortium collaboration', f'{name} ({alias}) partnered with {partner} on {activity} in {location}. The consortium agreement covers this pilot, not all operations of either organization.', date_new, 'partner'),
            (f'{name}: operational limitation', f'{name} ({alias}) noted that {activity} outcomes in {location} depend on local infrastructure and staged review. These limitations were documented separately from its {metric} headline number.', date_new, 'limitation'),
            (f'{name}: 2023 retrospective', f'An older {name} ({alias}) retrospective gave a 2023-06-30 {metric} value of {int(old*.72)} {unit}. Do not confuse the 2023 value with 2024 or the 2025 update.', '2023-06-30', 'metric'),
            (f'{name}: 2025 forecast not observation', f'A separate {name} ({alias}) forecast estimated {int(new*1.2)} {unit} of {metric} for 2025-12-31. This is a projection only, not the observed number on 2025-06-30.', '2025-06-15', 'forecast'),
            (f'{name}: earlier method trial', f'Before 2025, {name} ({alias}) considered rule-based monitoring as an alternative to {method} for {activity}. The alternative was not the selected technical method in the later pilot.', '2024-06-30', 'method'),
            (f'{name}: prior partner conversation', f'A 2024 scoping meeting between {name} ({alias}) and Ash Grove Forum covered possible {activity}. This was an informal discussion, not the signed pilot with {partner}.', '2024-06-30', 'partner'),
            (f'{name}: explicitly excluded solution', f'An audit of {name} ({alias}) {activity} distinguished {method} from an unrelated keyword-matching prototype. The unrelated prototype was rejected for deployment.', date_new, 'method'),
            (f'{name}: 2025 operations review', f'At {location}, {name} ({alias}) filed a 2025 operations review on {activity}; the review mentions {partner} only in the annex and includes no new signed agreement.', date_new, 'partner'),
            (f'{name}: preliminary measurement', f'A provisional {name} ({alias}) note gave {int(new*0.96)} {unit} of {metric} for 2025-05-31. This earlier provisional observation is not its final 2025-06-30 headline figure.', '2025-05-31', 'metric'),
        ]
        for j, (title, content, when, kind) in enumerate(rows, 1):
            corpus.append({**base, 'id': f'{key}-d{j}', 'report_id': f'fixture-{key}',
               'title':title, 'content':content, 'published_at':when,
               'source_url':f'https://research-{key}.example/report/{j}',
               'record_type':kind, 'data_origin':'fictional',
               'review_status':'not_real_world_verified'})
        for number, query, relevant, tag in [
          (1, f'What was {alias} {metric} on 2025-06-30, rather than the older 2024 number?', f'{key}-d2', 'temporal_latest'),
          (2, f'As of 2024-06-30, how much {metric} did {name} report before its subsequent revision?', f'{key}-d1', 'temporal_historical'),
          (3, f'Which technical approach supports {name} {activity} pilot in {sector}?', f'{key}-d3', 'technical_method'),
          (4, f'Who worked with {alias} on {activity} in {location}?', f'{key}-d4', 'entity_relation'),
        ]:
          if number == 3 and i % 3 == 0:
              query = ('\u8bf7\u8bf4\u660e ' + name + ' \u5728 ' + sector +
                       ' \u9879\u76ee\u4e2d\u91c7\u7528\u4e86\u54ea\u79cd\u6280\u672f\u65b9\u6cd5\uff1f')
          if number == 4 and i % 3 == 1:
              query = ('\u8c01\u4e0e ' + alias + ' \u5408\u4f5c\u5b8c\u6210 ' + activity + ' \u8bd5\u70b9\uff1f')
          negative_ids = {1:[1,6,7,12],2:[2,6,12],3:[8,10],4:[9,11]}[number]
          queries.append({'id':f'{key}-q{number}', 'query':query,
               'relevant_ids':[relevant], 'hard_negative_ids':[f'{key}-d{n}' for n in negative_ids], 'topic':sector,'category':tag,
               'synthetic':True,'language':'zh' if '\u8bf7' in query or '\u8c01' in query else 'en'})
    assert len(corpus) == 360 and len(queries) == 120

    tasks=[]
    for category, prompts in TASK_GROUPS.items():
        for p in prompts:
            tasks.append({'id':f'research-{len(tasks)+1:03d}', 'domain':category,
              'query':p, 'language':'en', 'difficulty':'multi_source',
              'evaluation_cutoff':'2025-12-31', 'requires_current_verification': True,
              'expected_aspects':ASPECTS[category], 'min_independent_sources':3,
              'status':'prompt_only_no_run', 'gold_report_available': False})
    assert len(tasks) == 60
    # Reuse four hand-authored Chinese prompts already shipped in the base repo.
    # This is a prompt catalog, not an evaluated data source or a gold answer.
    legacy_path = Path(__file__).resolve().parents[2] / 'tests' / 'eval_dataset.json'
    legacy = json.loads(legacy_path.read_text('utf-8'))['cases'][:4]
    for position, source in zip((0,12,30,48),legacy):
        tasks[position]['query'] = source['query']
        tasks[position]['expected_aspects'] = source['expected_aspects']
        tasks[position]['language'] = 'zh'
        tasks[position]['prompt_origin'] = 'repo_legacy_eval_dataset'
    faults=[]
    types = [
       ('research_worker_termination','research worker','before checkpoint', 'reclaim using lease and checkpoint'),
       ('memory_worker_termination','memory worker','after part of report memory saved', 'resume incomplete outbox steps without duplicated durable writes'),
       ('lease_expiration','job ownership','heartbeat stalls', 'one fenced owner, no stale confirmation'),
       ('redis_unavailability','task queue','intermittent connection loss', 'retry without dropping persisted task'),
       ('outbox_storage_failure','memory outbox','storage throws transient error', 'bounded retry and terminal dead-letter when necessary'),
    ]
    for key, name, point, expected in types:
        for index in range(1,9):
            faults.append({'id':f'{key}-{index:02d}','fault_type':key,'target':name,
                'injection_point':point,'retry_attempt':index,
                'expected_invariant':expected,'expected_no_lost_job':True,
                'expected_no_duplicate_write':True,'run_status':'planned_not_executed',
                'observed_recovered':None,'recovery_seconds':None})
    assert len(faults)==40
    dump_jsonl(ROOT/'memory_corpus.v1.jsonl', corpus)
    dump_jsonl(ROOT/'memory_queries.v1.jsonl', queries)
    dump_jsonl(ROOT/'research_tasks.v1.jsonl', tasks)
    dump_jsonl(ROOT/'fault_scenarios.v1.jsonl', faults)
    return {'memory_documents':len(corpus),'memory_queries':len(queries),
            'research_tasks':len(tasks),'fault_scenarios':len(faults)}

if __name__ == '__main__':
    print(json.dumps(build(), indent=2))
