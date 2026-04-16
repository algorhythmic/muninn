My read: this is Muninn (Odin's raven, "memory" — literally "the one who remembers"). Fits better than "Memex" 
  given the Saga naming convention, and the role maps cleanly: Muninn remembers everything you've ever read so
  Odin can recall it when you're brainstorming.                                                                  
                                                                                                                 
  Architecture I'd pitch: two layers, because a compiled wiki alone can't hold 10k+ pages, and a vector index    
  alone is unbrowsable by a human.                                                                               
                                                                                                                 
  - Layer 1 — Bragi-style vault. One page per bookmark: title, summary, key quotes, tags, original URL, capture  
  date, last-scraped date, archive.org fallback link. Obsidian-browsable, git-versioned. Dvergar do the scrape + 
  summarize in parallel (one dvergr per N bookmarks) — exactly the fleet workload Saga was built for.            
  - Layer 2 — semantic index. Embeddings of full-text + summaries in a local vector DB (Qdrant on a new LXC at
  .19 is the obvious slot). Exposed via an MCP server so Claude Code sessions can query it during brainstorms.   
  The vault is the human interface; the MCP is the LLM interface.
                                                                                                                 
  How Saga builds it: a spec ingests bookmarks.html, Odin decomposes into N parallel scrape-and-summarize        
  streams, dvergar work through their slices, Bragi compiles results into the vault. Phase 3 exercises the full
  pipeline on a real, large, messy corpus — which is exactly the test we need.                                   
                                                            
  Scope questions worth pinning down before I draft the spec:                                                    
   
  1. Source breadth. Bookmarks only, or also browser history / Pocket / HN favorites / Arena?                    
  2. Scrape depth. Readability text + meta, or full DOM + screenshots + YouTube transcripts?
  3. Dead-link strategy. archive.org fallback automatic, or flag-and-skip?                                       
  4. Sensitive-domain blocklist. Banking, medical, private docs — opt-in or opt-out?                             
  5. Freshness. One-time bulk ingest, or continuous (new bookmarks → auto-scrape)?                               
  6. Brainstorm integration. MCP server only, or also a Claude Code slash command (/muninn search ...)?          
                                                                                                                 
  Main tradeoffs to sit with:                                                                                    
                                                                                                                 
  - 10 years × thousands of URLs = real tokens. Sonnet-only summaries, or Haiku-first with Opus re-pass for the  
  ones you flag important?
  - Scraping ethics: robots.txt respect, rate-limiting per domain, no paywalled content.                         
  - Corpus curation: 10 years of bookmarks includes junk you never cared about — include a relevance-scoring     
  pass, or just index everything and let retrieval sort it out?                                                  
                                                                                                                 
  Which of those questions do you want to answer first, and does "Muninn" land?                                  
                                                            
✻ Crunched for 1m 1s                                                                                             
                                                                                          
❯ Munin can be a working title but we don't need to use norse references for all projects going forward. While   
  this can help provide context for Odin, it has many other practical uses so it can be its own naming           
  convention. For starting off I will provide a large bookmark file that has a series of nested folders with a   
  non-conventional dating system e.g. Jan 1, Feb 8, Mar 4. So based on the groupings of bookmarks by date we can 
   construct a timeline of sites visited, changing interestes, research and other endeavors, lots of conclusions 
   to draw from the data set. I think the important part is to clean and standardize the data so we can use it   
  in different ways. While saga can make use of it, I have ideas for many other ways to use the data so don't    
  confine ideas to the context of saga                                                                           
                                                                                                                 
● Right — then the spec should be data-pipeline-first, consumers-second. Muninn's core product is a clean,       
  normalized dataset; Saga/MCP/brainstorm retrieval are downstream consumers that happen to exist. Keeps future
  uses (timeline analyses, topic clustering, export-to-anything) open.                                           
                                                                                                                 
  Core insight from your "Jan 1, Feb 8" folder convention: the folder labels aren't the authoritative dates —    
  Netscape Bookmark HTML has ADD_DATE="<unix-ts>" attributes on every entry. So the real per-bookmark date is    
  already in the file. The folder labels become user-assigned era names, and we bracket each era's actual year   
  range by the min/max add_date of bookmarks inside it. That turns a "non-conventional" thing into a clean join  
  key.                                                                                                           

  Layered data model I'd propose:

  raw/          bookmarks.html committed untouched to git
    │                                                                                                            
    ▼
  normalized/   SQLite: one row per bookmark — url, title, folder_path[],                                        
                era_label ("Jan 1"), add_date, last_modified, domain.                                            
                This is the "clean dataset" you said was the important part.                                     
                Re-derivable from raw/ at any time.                                                              
    │                                                                                                            
    ▼                                                                                                            
  enriched/     same SQLite, more columns — scraped_at, scrape_status,                                           
                archive_url, content_text, content_html, summary,                                                
                key_quotes, tags, entities, language, word_count,                                                
                content_type (article/video/paper/tool/doc). Each enrichment                                     
                stage is idempotent + resumable; swap the summarizer prompt                                      
                and re-run stage 4 without re-scraping.                                                          
    │                                                                                                            
    ├─► FTS index (SQLite FTS5)                                                                                  
    ├─► Vector index (Qdrant on a new LXC at .19)           
    └─► eras/  derived table — per era: inferred_year, start/end date,                                           
              bookmark count, dominant topics, dominant domains, LLM-written                                     
              narrative ("what you were researching this period")                                                
                                                                                                                 
  consumers/    read from normalized/ + enriched/ + indexes. Independent:                                        
    • MCP server — semantic + FTS search for Claude Code brainstorms                                             
    • Obsidian vault — Bragi-style compilation, one page per bookmark                                            
    • CLI — ad hoc queries ("what I was reading about rust in mid-2022")                                         
    • Timeline view — interest-shift visualization                                                               
    • Parquet export — for future data-science work                                                              
                                                                                                                 
  Why SQLite for the store: portable single file, FTS5 built in, readable from anything (Python, Go, Rust,       
  Obsidian plugins, sqlite3 on your phone), easy to back up with cp. Not cloud-dependent. Parquet export covers
  the columnar-analytics case.                                                                                   
                                                            
  Saga's role, when it shows up: dvergar do stages 3 and 4 (scrape, summarize) in parallel against a sharded     
  bookmark queue. Odin decomposes the corpus into N streams; each dvergr works a slice, writes rows back to
  enriched/, commits to a Gitea repo. But Muninn-the-dataset stands alone — stage 2 is useful even with no LLM   
  enrichment.                                               

  Design decisions to pin before I draft the spec doc:                                                           
   
  1. Scraper strategy. HTTP + https://github.com/readability/readability covers 80% cheap. Playwright-fallback   
  for JS-heavy sites (Twitter, modern SPAs) adds weight. Start HTTP-only, add Playwright in a later stage?
  2. Dead-link policy. Auto-fetch from archive.org Wayback on 4xx/5xx, or flag-and-skip and let you triage? I'd  
  default to auto-fetch from Wayback; 10-year-old bookmarks = lots of dead links.                                
  3. Sensitive-domain handling. A skip_domains.yml you edit by hand (banking, medical, private docs). Opt-out is
  simpler than opt-in.                                                                                           
  4. LLM budget. Haiku for first-pass summaries on the whole 10k, Opus only for entries you flag important via
  the Obsidian vault? Or subscription-only Sonnet for everything and accept the token cost?                      
  5. Scope of v1. Bookmarks only, stop there, ship the dataset. Browser history / Pocket / HN / Arena all become
  separate ingest adapters in v2 that populate the same schema.                                                  
  6. Delivery. Is Muninn a monorepo (parser + scraper + enricher + MCP + UI all together) or multiple small repos
   sharing the SQLite schema as the contract? I'd lean monorepo at v1, split later.                              
                                                            
  Tradeoffs worth sitting with:                                                                                  
                                                            
  - Scraping politeness. Per-domain rate limit (≤1 rps), honest User-Agent, respect robots.txt, cache hits.      
  Ethics + pragmatism both point the same way.              
  - Corpus noise. 10 years of bookmarks includes "bookmarked once, never relevant." Don't filter at ingest — let 
  retrieval rank. You can tag noise as you browse the vault.                                                     
  - Schema stability. Get stage-2 schema right before enrichment runs start, because re-running the cheap stages
  is fine but re-running 10k summarizations hurts. Spec should nail column names/types before anything ships.    
                                                            
  Which of the six decisions do you want to settle first? I'd vote: (5) scope of v1 and (1) scraper strategy —   
  they're the biggest forks. Once those are in, I can draft the spec against the wiki template we just built and
  you can run it through Odin as Phase 3's first real project. 