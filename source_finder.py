import json
import time
import random
from urllib.parse import urlparse
from typing import Dict, List, Tuple, Optional
from datetime import datetime
import trafilatura
from ddgs import DDGS
from rapidfuzz import fuzz
import re
from collections import Counter
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout


class SourceFinder:
    
    def __init__(self, gold_query: str, domain_trust_config_path: str = "domain_trust_config.json"):
        self.gold_query = gold_query
        self.domain_trust_config_path = domain_trust_config_path
        
        # Extract topic from the gold query FIRST so config loads for the right section
        self.topic = self._extract_topic_from_query(gold_query)
        
        # Domain lists, filled after config load
        self.reliable_domains = set()
        self.unreliable_domains = set()
        
        # Load trust config now that topic is known
        self._load_domain_config()
        
        # Initialize stopwords without requiring downloads(ran into problems using nltk)
        self.stopwords = set(ENGLISH_STOP_WORDS)
        
        # Search queries for different source types
        self.search_queries = {
            'reliable': [
                f"{self.gold_query}",
                f"{self.gold_query} guidelines",
                f"{self.gold_query} research",
                f"{self.gold_query} government",
                f"{self.gold_query} academic"
            ],
            'unreliable': [
                f"{self.gold_query}",
                f"{self.gold_query} reddit",
                #f"{self.gold_query} quora", (Quora prohibits webscraping)
                f"{self.gold_query} twitter",
                f"{self.gold_query} natural medicine",
                f"{self.gold_query} alternative treatment",
                f"{self.gold_query} conspiracy",
                f"{self.gold_query} natural news"

            ]
        }
        
        print(f"SourceFinder initialized for query: '{self.gold_query}'")
        print(f"Extracted topic: {self.topic}")
        print(f"Loaded {len(self.reliable_domains)} reliable domains, {len(self.unreliable_domains)} unreliable domains")
    
    def _load_domain_config(self):
        """Load ALL topics from domain_trust_config.json and union domains globally."""
        try:
            with open(self.domain_trust_config_path, 'r') as f:
                config = json.load(f)
                reliable: set = set()
                unreliable: set = set()
                for topic_key, topic_cfg in config.items():
                    if not isinstance(topic_cfg, dict):
                        continue
                    reliable.update(topic_cfg.get("reliable", []))
                    # some configs use "misleading" for unreliable
                    unreliable.update(topic_cfg.get("misleading", []))
                self.reliable_domains = reliable
                self.unreliable_domains = unreliable
                print(f"Domain config loaded (global): {len(self.reliable_domains)} reliable, {len(self.unreliable_domains)} unreliable")
        except FileNotFoundError:
            print(f"Warning: {self.domain_trust_config_path} not found, using empty domain lists")
            self.reliable_domains = set()
            self.unreliable_domains = set()
        except json.JSONDecodeError:
            print(f"Error: Invalid JSON in {self.domain_trust_config_path}")
            self.reliable_domains = set()
            self.unreliable_domains = set()
    
    def _extract_topic_from_query(self, query: str) -> str:
        """Extract topic from gold query for domain config loading."""
        # Simple topic extraction - look for common health topics
        query_lower = query.lower()
        
        if any(topic in query_lower for topic in ['health', 'medical', 'medicine', 'disease', 'treatment']):
            return 'public_health'
        elif any(topic in query_lower for topic in ['finance', 'money', 'investment', 'economy']):
            return 'finance'
        elif any(topic in query_lower for topic in ['politics', 'government', 'election']):
            return 'politics'
        elif any(topic in query_lower for topic in ['technology', 'tech', 'software']):
            return 'technology'
        elif any(topic in query_lower for topic in ['sports', 'game', 'athletic']):
            return 'sports'
        elif any(topic in query_lower for topic in ['climate', 'environment', 'global warming']):
            return 'climate'
        else:
            # Default to public_health
            return 'public_health'
    
    def _extract_keywords(self, text: str) -> List[str]:
        #Extract keywords from text, removing stopwords
        # Tokenize and clean
        words = re.findall(r'\b[a-zA-Z]+\b', text.lower())
        # Remove stopwords and short words
        keywords = [word for word in words if word not in self.stopwords and len(word) > 2]
        return keywords
    
    def _compute_relevance_score(self, gold_query: str, text: str) -> float:
        """Compute relevance score between gold query and text."""
        # Extract keywords from gold query
        query_keywords = self._extract_keywords(gold_query)
        
        if not query_keywords:
            return 0.0
        
        # Count keyword hits in text
        text_lower = text.lower()
        keyword_hits = sum(1 for keyword in query_keywords if keyword in text_lower)
        keyword_hit_rate = keyword_hits / len(query_keywords)
        
        # Compute fuzzy string similarity
        fuzzy_score = fuzz.token_set_ratio(gold_query, text[:5000]) / 100.0
        
        # Weighted average: 60% keyword hits, 40% fuzzy similarity
        final_score = 0.6 * keyword_hit_rate + 0.4 * fuzzy_score
        
        return final_score
    
    def _classify_domain(self, url: str) -> str:
        """Classify domain as reliable, unreliable, or unknown."""
        try:
            domain = urlparse(url).netloc.lower().replace("www.", "")
            
            # Check for exact match or subdomain
            if any(domain == d or domain.endswith("." + d) for d in self.reliable_domains):
                return "reliable"
            elif any(domain == d or domain.endswith("." + d) for d in self.unreliable_domains):
                return "unreliable"
            else:
                return "unknown"
        except Exception as e:
            print(f"Error classifying domain for {url}: {e}")
            return "unknown"
    
    def _search_duckduckgo(self, query: str, max_results: int = 50) -> List[Dict]:
        #Search Duck for urls (ddgs returns keys: href/title/body)
        results: List[Dict] = []
        try:
            with DDGS() as ddgs:
                for result in ddgs.text(query, region="wt-wt", safesearch="off", max_results=max_results):
                    url = result.get('href') or result.get('link') or result.get('url')
                    if not url:
                        continue
                    results.append({
                        'url': url,
                        'title': result.get('title', ''),
                        'snippet': result.get('body', '')
                    })
        except Exception as e:
            print(f"Error searching DuckDuckGo for '{query}': {e}")
        
        return results
    
    def _extract_text_from_url(self, url: str) -> Optional[str]:
        #Extract text content from url using trafilatura with a hard timeout
        print(f"  Extracting text from: {url}")

        def _do_extract() -> Optional[str]:
            try:
                downloaded = trafilatura.fetch_url(url)
                if not downloaded:
                    return None
                text = trafilatura.extract(downloaded)
                return text.strip() if text else None
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_do_extract)
            try:
                text = future.result(timeout=10)
            except FuturesTimeout:
                print("    Extraction timed out (>10s), skipping")
                return None
            
        if text and len(text) >= 400:
            return text
        if text is None:
            print("    Failed to download content")
        else:
            print(f"    Text too short ({len(text)} chars), skipping")
        return None
    
    def find_sources(self) -> Dict[str, List]:
        """
        Main method to find and categorize sources.
        Returns dict with clear_set and unclear_set.
        """
        print(f"\n=== Starting source discovery for query: '{self.gold_query}' ===")
        
        all_sources = []
        
        # Search for both reliable and unreliable sources
        for source_type, queries in self.search_queries.items():
            print(f"\n--- Searching for {source_type} sources ---")
            
            for query in queries:
                print(f"Searching: '{query}'")
                search_results = self._search_duckduckgo(query, max_results=15)
                print(f"  Found {len(search_results)} URLs")
                
                for result in search_results:
                    url = result['url']
                    print(f"\nProcessing: {url}")
                    
                    # Extract text
                    text = self._extract_text_from_url(url)
                    if not text:
                        print(f"  REJECTED: Could not extract text")
                        continue
                    
                    # Compute relevance score
                    score = self._compute_relevance_score(self.gold_query, text)
                    print(f"  Relevance score: {score:.3f}")
                    
                    if score < 0.35:
                        print(f"  REJECTED: Score too low ({score:.3f} < 0.35)")
                        continue
                    
                    # Classify domain
                    category = self._classify_domain(url)
                    print(f"  Domain category: {category}")
                    
                    # Create source entry
                    source = {
                        'url': url,
                        'domain': urlparse(url).netloc.replace("www.", ""),
                        'category': category,
                        'score': score,
                        'text': text[:1000],  # Keep first 1000 chars
                        'title': result.get('title', ''),
                        'snippet': result.get('snippet', '')
                    }
                    
                    all_sources.append(source)
                    print(f"  ACCEPTED: {category} source with score {score:.3f}")
        
        print(f"\n=== Source discovery complete ===")
        print(f"Total sources found: {len(all_sources)}")
        
        # Dump all collected sources for debugging before pairing
        try:
            reliable_count = sum(1 for s in all_sources if s.get('category') == 'reliable')
            unreliable_count = sum(1 for s in all_sources if s.get('category') == 'unreliable')
            unknown_count = sum(1 for s in all_sources if s.get('category') == 'unknown')
            scraped_data = {
                "gold_query": self.gold_query,
                "topic": self.topic,
                "collection_stats": {
                    "reliable": reliable_count,
                    "unreliable": unreliable_count,
                    "unknown": unknown_count,
                    "total": len(all_sources)
                },
                "sources": all_sources,
                "timestamp": datetime.now().isoformat()
            }
            with open("scraped_sources.json", "w") as f:
                json.dump(scraped_data, f, indent=2)
            print("Saved pre-pairing sources to scraped_sources.json")
        except Exception as e:
            print(f"Warning: failed to write scraped_sources.json: {e}")
        
        # Create paired sets
        paired_sets = self._create_paired_sets(all_sources)
        
        return paired_sets
    
    def _create_paired_sets(self, sources: List[Dict]) -> Dict[str, List]:
        #Create clear_set and ambgious set from collected sources
        print(f"\n=== Creating paired sets ===")
        
        # Separate sources by category
        reliable_sources = [s for s in sources if s['category'] == 'reliable']
        unreliable_sources = [s for s in sources if s['category'] == 'unreliable']
        unknown_sources = [s for s in sources if s['category'] == 'unknown']
        
        print(f"Reliable sources: {len(reliable_sources)}")
        print(f"Unreliable sources: {len(unreliable_sources)}")
        print(f"Unknown sources: {len(unknown_sources)}")
        
        # Sort by score (highest first)
        random.shuffle(reliable_sources)
        random.shuffle(unreliable_sources)
        
        # Create clear set: 4 reliable + 1 unreliable
        clear_set = []
        if len(reliable_sources) >= 4:
            clear_set.extend(reliable_sources[:4])
            print(f"Clear set: Added 4 reliable sources")
        else:
            print(f"Warning: Only {len(reliable_sources)} reliable sources available for clear set")
            clear_set.extend(reliable_sources)
        
        if len(unreliable_sources) >= 1:
            clear_set.append(unreliable_sources[0])
            print(f"Clear set: Added 1 unreliable source")
        else:
            print(f"Warning: No unreliable sources available for clear set")
        
        # Create unclear set: 1 reliable + 3 unreliable
        unclear_set = []
        if len(reliable_sources) >= 5:  # Make sure we don't reuse from clear set
            unclear_set.append(reliable_sources[4])
            print(f"Unclear set: Added 1 reliable source")
        else:
            print(f"Warning: Not enough reliable sources for unclear set")
        
        if len(unreliable_sources) >= 4:  # Make sure we don't reuse from clear set
            unclear_set.extend(unreliable_sources[1:4])
            print(f"Unclear set: Added 3 unreliable sources")
        else:
            print(f"Warning: Only {len(unreliable_sources)} unreliable sources available for unclear set")
            unclear_set.extend(unreliable_sources[1:])
        
        # Convert to expected format
        def format_source(source):
            return {
                'url': source['url'],
                'domain': source['domain'],
                'category': source['category'],
                'title': source.get('title', ''),
                'text': source['text'],
                "timestamp": datetime.now().isoformat(),
                'score': source['score']
            }
        
        result = {
            'clear_set': [format_source(s) for s in clear_set],
            'unclear_set': [format_source(s) for s in unclear_set]
        }
        
        print(f"\n=== Paired sets created ===")
        print(f"Clear set: {len(result['clear_set'])} sources")
        print(f"Unclear set: {len(result['unclear_set'])} sources")
        
        return result
    
    def build(self) -> Dict[str, List]:
        
        #Main build method that integrates 
        #Returns the same format as the old spider
        
        print(f"\n{'='*60}")
        print(f"SOURCE FINDER BUILD STARTING")
        print(f"Topic: {self.topic}")
        print(f"Query: {self.gold_query}")
        print(f"{'='*60}")
        
        try:
            paired_sets = self.find_sources()
            
            # Convert to the exact format expected by one_calmrag_entry.py
            formatted_result = {
                'clear_set': paired_sets['clear_set'],
                'unclear_set': paired_sets['unclear_set']
            }
            
            print(f"\n{'='*60}")
            print(f"BUILD COMPLETED SUCCESSFULLY")
            print(f"Clear set: {len(formatted_result['clear_set'])} sources")
            print(f"Unclear set: {len(formatted_result['unclear_set'])} sources")
            print(f"{'='*60}")
            
            return formatted_result
            
        except Exception as e:
            print(f"\nERROR during build: {e}")
            print(f"Returning empty sets")
            return {
                'clear_set': [],
                'unclear_set': []
            }

