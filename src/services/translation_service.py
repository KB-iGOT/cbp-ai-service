import json
from typing import List, Dict, Optional
import ssl
import aiohttp
from ..core.logger import logger
from ..core.configs import settings


class TranslationService:
    """Service for translating text using Bhashini APIs"""
    
    def __init__(self):
        self.user_id = settings.BHASHINI_USER_ID
        self.api_key = settings.BHASHINI_API_KEY
        self.pipeline_id = settings.BHASHINI_PIPELINE_ID
        self.config_api_url = settings.BHASHINI_CONFIG_API_URL
        self.supported_languages = settings.BHASHINI_SUPPORTED_LANGUAGES
        
        if not self.user_id or not self.api_key:
            logger.warning("Bhashini credentials not configured. Translation service will be disabled.")
    
    def is_enabled(self) -> bool:
        """Check if translation service is properly configured"""
        return bool(self.user_id and self.api_key)
    
    async def get_translation_config(
        self,
        task: str,
        source_language: str,
        target_language: str | None = None
    ) -> Optional[Dict]:
        """Get translation configuration from Bhashini"""
        if not self.is_enabled():
            logger.error("Translation service not configured")
            return None
        
        headers = {
            "userID": self.user_id,
            "ulcaApiKey": self.api_key,
            "Content-Type": "application/json"
        }

        if task in ["asr", "tts"]:
            payload = {
                    "pipelineTasks": [
                        {
                            "taskType": task,
                            "config": {"language": {"sourceLanguage": source_language}},
                        }
                    ],
                    "pipelineRequestConfig": {
                        "pipelineId": self.pipeline_id
                    }
                }
        else:
            payload = {
                "pipelineTasks": [
                    {
                        "taskType": "translation",
                        "config": {
                            "language": {
                                "sourceLanguage": source_language,
                                "targetLanguage": target_language
                            }
                        }
                    }
                ],
                "pipelineRequestConfig": {
                    "pipelineId": self.pipeline_id
                }
            }
        
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                async with session.post(
                    self.config_api_url,
                    headers=headers,
                    json=payload
                    # timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    if response.status == 200:
                        config = await response.json()
                        logger.debug(f"Successfully retrieved translation config for {source_language} -> {target_language}")
                        return config
                    else:
                        error_text = await response.text()
                        logger.error(f"Failed to get translation config: {response.status} - {error_text}")
                        return None
        except Exception as e:
            logger.error(f"Error getting translation config: {str(e)}")
            return None
    
    async def translate_batch(
        self,
        texts: List[str],
        source_language: str,
        target_language: str
    ) -> List[str]:
        """Translate a batch of texts"""
        if not self.is_enabled():
            logger.warning("Translation service not configured, returning original texts")
            return texts
        
        if not texts:
            return []
        
        # Step 1: Get translation configuration
        config = await self.get_translation_config(task="translation", source_language=source_language, target_language=target_language)
        
        if not config:
            logger.error("Failed to get translation config, returning original texts")
            return texts
        
        # Step 2: Extract values from config response
        try:
            pipeline_config = config.get("pipelineResponseConfig", [{}])[0]
            taskType = pipeline_config.get("taskType", "translation")
            pipelineTasks_config = pipeline_config.get("config", [{}])[0]

            # Get API endpoint and authorization
            inference_endpoint = config.get("pipelineInferenceAPIEndPoint", {})
            translation_api_url = inference_endpoint.get("callbackUrl", "https://dhruva-api.bhashini.gov.in/services/inference/pipeline")
            auth_key_name = inference_endpoint.get("inferenceApiKey", {}).get("name", "Authorization")
            auth_key_value = inference_endpoint.get("inferenceApiKey", {}).get("value", "")
            
        except Exception as e:
            logger.error(f"Error parsing config response: {str(e)}")
            return texts
        
        # Step 3: Prepare translation request
        headers = {
            auth_key_name: auth_key_value,
            "Content-Type": "application/json"
        }
        
        payload = {
            "pipelineTasks": [{
                "taskType": taskType,
                "config": pipelineTasks_config
            }],
            "inputData": {
                "input": [{"source": text} for text in texts]
            }
        }
        
        # Step 4: Call translation API
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                async with session.post(
                    translation_api_url,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        translations = [
                            item.get("target", texts[i])
                            for i, item in enumerate(result.get("pipelineResponse", [{}])[0].get("output", []))
                        ]
                        logger.debug(f"Successfully translated {len(translations)} texts to {target_language}")
                        return translations
                    else:
                        error_text = await response.text()
                        logger.error(f"Translation API error: {response.status} - {error_text}")
                        return texts  # Return original texts on error
        except Exception as e:
            logger.error(f"Error during translation: {str(e)}")
            return texts  # Return original texts on error


# Global instance
translation_service = TranslationService()
