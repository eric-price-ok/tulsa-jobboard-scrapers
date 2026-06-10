def test_api_connection(self) -> bool:
    """Test API connection with different strategies"""
    try:
        logger.info("Testing API connection...")
        
        # First visit the jobboard to establish session
        logger.info("Visiting jobboard page to establish session...")
        response = self.session.get(self.config['jobboard_url'])
        logger.info(f"Jobboard page status: {response.status_code}")
        
        self.working_endpoint = "https://my.adp.com/myadp_prefix/mycareer/public/staffing/v1/job-requisitions/apply-custom-filters"
        self.orgoid = "G3QYFRD3XMX2K21G"
        
        # Try different API strategies
        strategies = [
            {
                'name': 'No postingChannelId',
                'params': {
                    '$orderby': 'postingDate desc',
                    '$select': 'reqId,jobTitle,publishedJobTitle,type,jobDescription,jobQualifications,workLocations,workLevelCode,clientRequisitionID,postingDate,requisitionLocations',
                    '$top': '5',
                    'tz': 'America/Chicago'
                }
            },
            {
                'name': 'With empty postingChannelId',
                'params': {
                    '$orderby': 'postingDate desc',
                    '$select': 'reqId,jobTitle,publishedJobTitle,type,jobDescription,jobQualifications,workLocations,workLevelCode,clientRequisitionID,postingDate,requisitionLocations',
                    '$top': '5',
                    'tz': 'America/Chicago',
                    'postingChannelId': ''
                }
            },
            {
                'name': 'Different endpoint - no filters',
                'endpoint': 'https://my.adp.com/myadp_prefix/mycareer/public/staffing/v1/job-requisitions',
                'params': {
                    '$top': '5',
                    'tz': 'America/Chicago'
                }
            }
        ]
        
        for strategy in strategies:
            logger.info(f"Trying strategy: {strategy['name']}")
            
            endpoint = strategy.get('endpoint', self.working_endpoint)
            params = strategy['params']
            
            headers = {
                'accept': 'application/json, text/plain, */*',
                'accept-language': 'en-US',
                'orgoid': self.orgoid,
                'rolecode': 'manager',
                'referer': 'https://myjobs.adp.com/',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-site',
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0'
            }
            
            response = self.session.get(endpoint, params=params, headers=headers)
            
            logger.info(f"  Status: {response.status_code}")
            
            if response.status_code == 200:
                logger.info(f"✓ SUCCESS with strategy: {strategy['name']}")
                return True
            else:
                logger.info(f"  Failed: {response.text[:200]}...")
        
        logger.error("✗ All strategies failed")
        return False
            
    except Exception as e:
        logger.error(f"✗ Connection test failed: {e}")
        return False