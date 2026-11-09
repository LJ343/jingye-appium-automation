package org.json;
import java.util.LinkedHashMap;
/** Minimal serializer for testing StateStore's selected fields, not a shipped JSON implementation. */
public class JSONObject {
    private final LinkedHashMap<String,Object> values=new LinkedHashMap<>();
    public JSONObject put(String key,Object value)throws JSONException{values.put(key,value);return this;}
    public String toString(){StringBuilder s=new StringBuilder("{");for(String key:values.keySet()){if(s.length()>1)s.append(',');Object value=values.get(key);s.append('"').append(key).append("\":");if(value instanceof String)s.append('"').append(value).append('"');else s.append(value);}return s.append('}').toString();}
}
