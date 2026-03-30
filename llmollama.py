from ollama import chat

def ollama_stream(user_prompt: str):
    system_prompt = f"""
    Ты крутой бот помощник Джарвис Тони Старка, общайся с кайфом анализируя текст пользователя: {user_prompt}
    """
    
    stream = chat(
        model='deepseek-llm',
        messages=[{
            'role': 'system', 'content': system_prompt
            }, 
                  {
                      'role': 'user', 'content': user_prompt
                      }],
        stream=False
    )
    
    content = stream['message']['content']
    return content