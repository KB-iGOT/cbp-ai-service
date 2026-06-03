import psycopg2
import requests
import logging
from typing import List, Dict, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class CourseUpdater:
    def __init__(self, db_config):
        self.db_config = db_config
        self.api_url = 'https://portal.igotkarmayogi.gov.in/api/content/v1/search'
        self.conn = psycopg2.connect(**db_config)
    
    def call_content_api_bulk(self, identifiers: List[str]) -> Dict[str, Dict]:
        """
        Call the external content API with multiple identifiers.
        Returns a dictionary mapping identifier -> content dict
        """
        payload = {
            "request": {
                "filters": {
                    "primaryCategory": ["Course"],
                    "status": ["Live"],
                    "courseCategory": ["Course"],
                    "identifier": identifiers
                },
                "fields": ["name"]
            }
        }
        
        headers = {'Content-Type': 'application/json'}
        result_map = {}
        
        try:
            response = requests.post(self.api_url, json=payload, headers=headers, timeout=60)
            response.raise_for_status()
            data = response.json()
            
            if data.get('responseCode') == 'OK' and data.get('result', {}).get('content'):
                for item in data['result']['content']:
                    ident = item.get('identifier')
                    if ident:
                        result_map[ident] = item
            else:
                logger.warning("No content returned for bulk request")
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Bulk API call failed: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in bulk API call: {e}")
        
        return result_map
    
    def update_course_names_bulk(self, batch_size: int = 50):
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT id, identifier FROM course_metadata_v2")
            courses = cursor.fetchall()
            
            # Split identifiers into batches
            for i in range(0, len(courses), batch_size):
                batch = courses[i:i+batch_size]
                identifiers = [identifier for _, identifier in batch]
                
                content_map = self.call_content_api_bulk(identifiers)
                
                update_data = []
                for course_id, identifier in batch:
                    content = content_map.get(identifier)
                    if content and "name" in content:
                        update_data.append((content["name"], course_id))
                        logger.info(f"Prepared update for {identifier} with name {content['name']}")
                
                if update_data:
                    cursor.executemany(
                        "UPDATE course_metadata_v2 SET name = %s WHERE id = %s",
                        update_data
                    )
                    self.conn.commit()
                    logger.info(f"Batch {i//batch_size + 1} updated successfully.")
        
        logger.info(f"Total courses fetched from DB ::  {len(courses)}")
        logger.info("All courses updated successfully.")

if __name__ == "__main__":
    db_config = {
        'host': '',
        'dbname': '',
        'user': '',
        'password': '',
        'port': 5432
    }
    
    updater = CourseUpdater(db_config)
    updater.update_course_names_bulk()
