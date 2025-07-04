import os
import requests
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from celery import current_task
from dotenv import load_dotenv

# Import shared components
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '../../../'))
from shared.database.connection import SessionLocal
from shared.models.database_models import DataSource, FeedItem, IngestionJob, ContentCache, UserCategory, UserDB
from celery_app import celery_app

load_dotenv()

class PerplexityRunner:
    """Runner for Perplexity API integration"""
    
    def __init__(self):
        self.api_key = os.getenv("PERPLEXITY_API_KEY")
        self.base_url = "https://api.perplexity.ai/chat/completions"
        self.db = SessionLocal()
        
    def get_data_source(self) -> Optional[DataSource]:
        """Get or create Perplexity data source"""
        data_source = self.db.query(DataSource).filter(
            DataSource.name == "perplexity"
        ).first()
        
        if not data_source:
            data_source = DataSource(
                name="perplexity",
                display_name="Perplexity AI",
                base_url=self.base_url,
                rate_limit_per_minute=60,
                config={"model": "llama-3.1-sonar-small-128k-online"}
            )
            self.db.add(data_source)
            self.db.commit()
            self.db.refresh(data_source)
        
        return data_source if data_source.is_active else None
    
    def get_user_categories(self, user_id: int) -> List[UserCategory]:
        """Get all active categories for a specific user"""
        return self.db.query(UserCategory).filter(
            UserCategory.user_id == user_id,
            UserCategory.is_active == True
        ).all()
    
    def generate_personalized_queries(self, user_categories: List[UserCategory]) -> List[str]:
        """Generate personalized queries based on user categories"""
        queries = []
        
        for category in user_categories:
            keywords = category.keywords or []
            category_name = category.category_name
            
            if keywords:
                # Create query using category keywords
                keyword_query = f"What are the latest news and developments about {', '.join(keywords)}?"
                queries.append({
                    "query": keyword_query,
                    "category_id": category.id,
                    "category_name": category_name,
                    "user_id": category.user_id
                })
            else:
                # Fallback to category name
                fallback_query = f"What are the latest news and developments in {category_name}?"
                queries.append({
                    "query": fallback_query,
                    "category_id": category.id,
                    "category_name": category_name,
                    "user_id": category.user_id
                })
        
        return queries
    
    def generate_fallback_queries(self) -> List[Dict[str, Any]]:
        """Generate fallback queries when no user categories are available"""
        fallback_queries = [
            "What are the top technology news stories today?",
            "What are the major world events happening right now?",
            "What are the latest developments in AI and machine learning?",
            "What are the trending topics in science and research?",
            "What are the key business and finance news today?"
        ]
        
        return [
            {
                "query": query,
                "category_id": None,
                "category_name": "General",
                "user_id": None
            }
            for query in fallback_queries
        ]
    
    def create_cache_key(self, query: str, model: str) -> str:
        """Create cache key for query"""
        import hashlib
        cache_data = f"{query}:{model}:{datetime.utcnow().strftime('%Y-%m-%d')}"
        return hashlib.md5(cache_data.encode()).hexdigest()
    
    def get_cached_response(self, cache_key: str) -> Optional[Dict]:
        """Get cached response if available and not expired"""
        cache_entry = self.db.query(ContentCache).filter(
            ContentCache.cache_key == cache_key,
            ContentCache.data_source == "perplexity",
            ContentCache.expires_at > datetime.utcnow()
        ).first()
        
        return cache_entry.response_data if cache_entry else None
    
    def cache_response(self, cache_key: str, response_data: Dict, expire_hours: int = 24):
        """Cache API response"""
        expires_at = datetime.utcnow() + timedelta(hours=expire_hours)
        
        cache_entry = ContentCache(
            cache_key=cache_key,
            data_source="perplexity",
            response_data=response_data,
            expires_at=expires_at
        )
        
        self.db.add(cache_entry)
        self.db.commit()
    
    def query_perplexity(self, query: str, model: str = "llama-3.1-sonar-small-128k-online") -> Optional[Dict]:
        """Query Perplexity API"""
        if not self.api_key:
            print("PERPLEXITY_API_KEY not found in environment")
            return None
        
        # Check cache first
        cache_key = self.create_cache_key(query, model)
        cached_response = self.get_cached_response(cache_key)
        if cached_response:
            return cached_response
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a helpful assistant that provides concise, informative summaries of current events and trending topics. Focus on factual information and provide relevant context."
                },
                {
                    "role": "user",
                    "content": query
                }
            ],
            "max_tokens": 1000,
            "temperature": 0.7
        }
        
        try:
            response = requests.post(self.base_url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            
            result = response.json()
            
            # Cache the response
            self.cache_response(cache_key, result)
            
            return result
            
        except requests.exceptions.RequestException as e:
            print(f"Error querying Perplexity API: {e}")
            return None
    
    def extract_content_from_response(self, response: Dict) -> List[Dict[str, Any]]:
        """Extract structured content from Perplexity response"""
        content_items = []
        
        try:
            if "choices" in response and len(response["choices"]) > 0:
                content = response["choices"][0]["message"]["content"]
                
                # Parse the content and extract structured information
                # This is a simplified parser - you might want to make it more sophisticated
                lines = content.split('\n')
                current_item = {}
                
                for line in lines:
                    line = line.strip()
                    if not line:
                        if current_item:
                            content_items.append(current_item)
                            current_item = {}
                        continue
                    
                    # Simple parsing logic - adjust based on actual response format
                    if line.startswith('**') and line.endswith('**'):
                        if current_item:
                            content_items.append(current_item)
                        current_item = {"title": line.strip('*')}
                    elif line.startswith('http'):
                        current_item["url"] = line
                    elif len(line) > 50:  # Likely content
                        current_item["summary"] = line
                
                if current_item:
                    content_items.append(current_item)
            
        except Exception as e:
            print(f"Error extracting content from response: {e}")
        
        return content_items
    
    def save_feed_items(self, content_items: List[Dict], data_source: DataSource, category_info: Dict[str, Any] = None) -> Dict[str, int]:
        """Save extracted content as feed items with category association"""
        created = 0
        updated = 0
        
        category_name = category_info.get("category_name", "General") if category_info else "General"
        category_id = category_info.get("category_id") if category_info else None
        user_id = category_info.get("user_id") if category_info else None
        
        for item in content_items:
            try:
                # Check if item already exists (by title and source)
                existing_item = self.db.query(FeedItem).filter(
                    FeedItem.title == item.get("title"),
                    FeedItem.data_source_id == data_source.id
                ).first()
                
                if existing_item:
                    # Update existing item
                    existing_item.summary = item.get("summary", existing_item.summary)
                    existing_item.url = item.get("url", existing_item.url)
                    existing_item.updated_at = datetime.utcnow()
                    updated += 1
                else:
                    # Create new item with category association
                    feed_item = FeedItem(
                        title=item.get("title", "Untitled"),
                        summary=item.get("summary", ""),
                        url=item.get("url", ""),
                        source="Perplexity AI",
                        data_source_id=data_source.id,
                        published_at=datetime.utcnow(),
                        raw_data=item,
                        category=category_name,
                        tags=["ai", "perplexity", category_name.lower()] if category_name != "General" else ["ai", "perplexity"]
                    )
                    
                    # Add user-specific metadata if available
                    if user_id:
                        feed_item.raw_data = {
                            **item,
                            "user_category_id": category_id,
                            "user_id": user_id,
                            "category_name": category_name
                        }
                    
                    self.db.add(feed_item)
                    created += 1
                
            except Exception as e:
                print(f"Error saving feed item: {e}")
                continue
        
        self.db.commit()
        return {"created": created, "updated": updated}

@celery_app.task(bind=True)
def ingest_perplexity(self, user_id: Optional[int] = None, queries: List[Dict[str, Any]] = None):
    """Celery task for Perplexity ingestion with personalized queries"""
    runner = PerplexityRunner()
    data_source = runner.get_data_source()
    
    if not data_source:
        print("Perplexity data source not found or inactive")
        return {"error": "Data source not found"}
    
    # Generate queries based on user categories or use fallback
    if queries is None:
        if user_id:
            # Get personalized queries for specific user
            user_categories = runner.get_user_categories(user_id)
            if user_categories:
                queries = runner.generate_personalized_queries(user_categories)
                print(f"Generated {len(queries)} personalized queries for user {user_id}")
            else:
                # No user categories, use fallback
                queries = runner.generate_fallback_queries()
                print(f"No user categories found for user {user_id}, using fallback queries")
        else:
            # No user specified, use fallback
            queries = runner.generate_fallback_queries()
            print("No user specified, using fallback queries")
    
    # Create ingestion job record
    job = IngestionJob(
        job_type="perplexity",
        status="running",
        started_at=datetime.utcnow(),
        parameters={"user_id": user_id, "queries": [q.get("query", q) if isinstance(q, dict) else q for q in queries]},
        data_source_id=data_source.id
    )
    runner.db.add(job)
    runner.db.commit()
    
    total_created = 0
    total_updated = 0
    
    try:
        for i, query_info in enumerate(queries):
            if isinstance(query_info, dict):
                query = query_info["query"]
                category_info = query_info
            else:
                query = query_info
                category_info = {"category_name": "General", "category_id": None, "user_id": None}
            
            # Update task progress
            self.update_state(
                state="PROGRESS",
                meta={
                    "current_query": query, 
                    "processed": i + 1, 
                    "total": len(queries),
                    "category": category_info.get("category_name", "General")
                }
            )
            
            response = runner.query_perplexity(query)
            if response:
                content_items = runner.extract_content_from_response(response)
                results = runner.save_feed_items(content_items, data_source, category_info)
                total_created += results["created"]
                total_updated += results["updated"]
        
        # Update job record
        job.status = "completed"
        job.completed_at = datetime.utcnow()
        job.items_created = total_created
        job.items_updated = total_updated
        runner.db.commit()
        
        return {
            "status": "completed",
            "created": total_created,
            "updated": total_updated,
            "queries_processed": len(queries),
            "user_id": user_id
        }
        
    except Exception as e:
        # Update job record with error
        job.status = "failed"
        job.completed_at = datetime.utcnow()
        job.error_message = str(e)
        runner.db.commit()
        
        raise self.retry(countdown=60, max_retries=3)
    
    finally:
        runner.db.close()

@celery_app.task(bind=True)
def ingest_perplexity_for_all_users(self):
    """Celery task for Perplexity ingestion for all users with categories"""
    runner = PerplexityRunner()
    data_source = runner.get_data_source()
    
    if not data_source:
        print("Perplexity data source not found or inactive")
        return {"error": "Data source not found"}
    
    # Get all users with categories
    users_with_categories = runner.db.query(UserDB).join(UserCategory).filter(
        UserCategory.is_active == True
    ).distinct().all()
    
    total_created = 0
    total_updated = 0
    users_processed = 0
    
    # Create ingestion job record
    job = IngestionJob(
        job_type="perplexity_all_users",
        status="running",
        started_at=datetime.utcnow(),
        parameters={"users_count": len(users_with_categories)},
        data_source_id=data_source.id
    )
    runner.db.add(job)
    runner.db.commit()
    
    try:
        for user in users_with_categories:
            # Update task progress
            self.update_state(
                state="PROGRESS",
                meta={
                    "current_user": user.username,
                    "processed_users": users_processed + 1,
                    "total_users": len(users_with_categories)
                }
            )
            
            # Ingest for this user
            result = ingest_perplexity.delay(user.id)
            user_result = result.get(timeout=300)  # 5 minute timeout per user
            
            if user_result and "created" in user_result:
                total_created += user_result["created"]
                total_updated += user_result["updated"]
                users_processed += 1
        
        # Update job record
        job.status = "completed"
        job.completed_at = datetime.utcnow()
        job.items_created = total_created
        job.items_updated = total_updated
        runner.db.commit()
        
        return {
            "status": "completed",
            "created": total_created,
            "updated": total_updated,
            "users_processed": users_processed,
            "total_users": len(users_with_categories)
        }
        
    except Exception as e:
        # Update job record with error
        job.status = "failed"
        job.completed_at = datetime.utcnow()
        job.error_message = str(e)
        runner.db.commit()
        
        raise self.retry(countdown=60, max_retries=3)
    
    finally:
        runner.db.close()

if __name__ == "__main__":
    # Test the runner
    runner = PerplexityRunner()
    result = runner.query_perplexity("What are the top technology news stories today?")
    print(json.dumps(result, indent=2)) 